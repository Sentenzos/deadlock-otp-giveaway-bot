from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.main import pick_winners
from app.storage import Storage
from app.twitch_chat import TwitchChatState, parse_tags


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temporary_directory.name) / "bot.sqlite3")
        await self.storage.connect()

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self.temporary_directory.cleanup()

    async def test_candidate_requires_link_time_and_messages(self) -> None:
        giveaway = await self.storage.start_giveaway(60, 3)
        code = await self.storage.create_link_code(
            101, "Alice", giveaway.id, "alice_tg"
        )
        status, telegram_id, _ = await self.storage.claim_link_code(code, "alice_tv", "777")
        self.assertEqual((status, telegram_id), ("linked", 101))

        await self.storage.mark_joined("alice_tv", "777")
        await self.storage.record_message("alice_tv", "777")
        await self.storage.record_message("alice_tv", "777")
        await self.storage.record_message("alice_tv", "777")
        await self.storage._db.execute(
            """UPDATE giveaway_activity SET seconds = 3600, presence_started_at = NULL
               WHERE giveaway_id = ? AND twitch_login = 'alice_tv'""",
            (giveaway.id,),
        )
        await self.storage._db.commit()

        candidates = await self.storage.eligible_candidates(giveaway)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].telegram_user_id, 101)
        self.assertEqual(candidates[0].telegram_username, "alice_tg")
        self.assertEqual(candidates[0].messages, 3)

    async def test_activity_recorded_before_link_is_kept_after_registration(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 1)
        await self.storage.record_message("alice_tv", "777")
        await self.storage._db.execute(
            """UPDATE giveaway_activity
               SET seconds = 60, presence_started_at = NULL
               WHERE giveaway_id = ? AND twitch_login = 'alice_tv'""",
            (giveaway.id,),
        )
        await self.storage._db.commit()

        code = await self.storage.create_link_code(101, "Alice", giveaway.id)
        status, telegram_id, _ = await self.storage.claim_link_code(
            code, "alice_tv", "777"
        )
        stats = await self.storage.participant_stats(giveaway, 101)

        self.assertEqual((status, telegram_id), ("linked", 101))
        self.assertIsNotNone(stats)
        self.assertEqual((stats.seconds, stats.messages), (60, 1))
        candidates = await self.storage.eligible_candidates(giveaway)
        self.assertEqual([candidate.twitch_login for candidate in candidates], ["alice_tv"])

    async def test_link_message_can_open_presence_without_counting_a_comment(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 1)
        await self.storage.record_message(
            "alice_tv", "777", count_message=False, count_time=True
        )
        await self.storage._db.execute(
            """UPDATE giveaway_activity
               SET presence_started_at = presence_started_at - 60
               WHERE giveaway_id = ? AND twitch_login = 'alice_tv'""",
            (giveaway.id,),
        )
        await self.storage._db.commit()

        code = await self.storage.create_link_code(101, "Alice", giveaway.id)
        await self.storage.claim_link_code(code, "alice_tv", "777")
        stats = await self.storage.participant_stats(giveaway, 101)

        self.assertIsNotNone(stats)
        self.assertGreaterEqual(stats.seconds, 60)
        self.assertEqual(stats.messages, 0)

    async def test_repeated_link_request_reuses_the_unexpired_code(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 1)

        first = await self.storage.create_link_code(
            101, "Alice", giveaway.id, "old_name"
        )
        second = await self.storage.create_link_code(
            101, "Alice Updated", giveaway.id, "new_name"
        )

        self.assertEqual(second, first)
        rows = await (
            await self.storage._db.execute(
                """SELECT telegram_name, telegram_username FROM link_codes
                   WHERE telegram_user_id = ? AND giveaway_id = ?""",
                (101, giveaway.id),
            )
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["telegram_name"], rows[0]["telegram_username"]),
            ("Alice Updated", "new_name"),
        )

    async def test_simultaneous_link_requests_return_one_shared_code(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 1)

        codes = await asyncio.gather(
            *(
                self.storage.create_link_code(
                    101, "Alice", giveaway.id, "alice_tg"
                )
                for _ in range(8)
            )
        )

        self.assertEqual(len(set(codes)), 1)
        row = await (
            await self.storage._db.execute(
                "SELECT COUNT(*) AS total FROM link_codes WHERE telegram_user_id = 101"
            )
        ).fetchone()
        self.assertEqual(row["total"], 1)

    async def test_winner_is_selected_only_from_fully_qualified_people(self) -> None:
        giveaway = await self.storage.start_giveaway(10, 2)
        people = (
            (101, "Alice", "alice_tv", "777"),
            (102, "Bob", "bob_tv", "778"),
            (103, "Cara", "cara_tv", "779"),
        )
        for telegram_id, name, login, twitch_id in people:
            code = await self.storage.create_link_code(telegram_id, name, giveaway.id)
            await self.storage.claim_link_code(code, login, twitch_id)
            await self.storage.record_message(login, twitch_id)
        await self.storage.record_message("alice_tv", "777")
        await self.storage.record_message("bob_tv", "778")
        await self.storage._db.execute(
            """UPDATE giveaway_activity
               SET seconds = CASE twitch_login
                   WHEN 'alice_tv' THEN 600
                   WHEN 'bob_tv' THEN 599
                   WHEN 'cara_tv' THEN 600
                   END,
                   presence_started_at = NULL
               WHERE giveaway_id = ?""",
            (giveaway.id,),
        )
        await self.storage._db.commit()

        candidates = await self.storage.eligible_candidates(giveaway)
        winners = pick_winners(candidates, 1)

        self.assertEqual([candidate.twitch_login for candidate in candidates], ["alice_tv"])
        self.assertEqual([winner.twitch_login for winner in winners], ["alice_tv"])

    async def test_winner_is_excluded_when_rerolling(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 1)
        for telegram_id, login, twitch_id in ((101, "alice_tv", "777"), (102, "bob_tv", "778")):
            code = await self.storage.create_link_code(telegram_id, login, giveaway.id)
            await self.storage.claim_link_code(code, login, twitch_id)
        for login, twitch_id in (("alice_tv", "777"), ("bob_tv", "778")):
            await self.storage.record_message(login, twitch_id)
        await self.storage._db.execute(
            "UPDATE giveaway_activity SET seconds = 60, presence_started_at = NULL WHERE giveaway_id = ?",
            (giveaway.id,),
        )
        await self.storage._db.commit()
        await self.storage.record_winner(giveaway.id, 101)

        candidates = await self.storage.eligible_candidates(giveaway)
        self.assertEqual([candidate.telegram_user_id for candidate in candidates], [102])

    async def test_giveaway_keeps_title_winner_count_and_participants(self) -> None:
        end_at = int(self.storage.now()) + 86_400
        giveaway = await self.storage.start_giveaway(
            10,
            2,
            3,
            "Steam ключи",
            "Deadlock набор",
            min_participants=10,
            end_at=end_at,
        )
        code = await self.storage.create_link_code(101, "Alice", giveaway.id)
        await self.storage.claim_link_code(code, "alice_tv", "777")
        self.assertEqual(giveaway.title, "Steam ключи")
        self.assertEqual(giveaway.prize, "Deadlock набор")
        self.assertEqual(giveaway.winner_count, 3)
        self.assertEqual(giveaway.min_participants, 10)
        self.assertEqual(giveaway.end_at, end_at)
        loaded = await self.storage.active_giveaway()
        self.assertEqual(loaded.end_at, end_at)

        await self.storage.record_message("alice_tv", "777")
        await self.storage.record_message("guest_tv", "888")
        await self.storage._db.execute(
            """UPDATE giveaway_activity SET seconds = 600, presence_started_at = NULL
               WHERE giveaway_id = ?""",
            (giveaway.id,),
        )
        await self.storage._db.commit()

        participants = await self.storage.giveaway_participants(giveaway)
        self.assertEqual([participant.twitch_login for participant in participants], ["alice_tv"])
        self.assertEqual(participants[0].telegram_user_id, 101)

    async def test_twitch_announcements_are_off_by_default_and_persist_configuration(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 1, title="Twitch-анонсы")

        self.assertFalse(giveaway.twitch_announce_enabled)
        self.assertEqual(giveaway.twitch_announce_interval_seconds, 900)
        self.assertIsNone(giveaway.twitch_last_announce_at)

        updated = await self.storage.configure_twitch_announcements(
            enabled=True, interval_minutes=15
        )
        self.assertTrue(updated.twitch_announce_enabled)
        self.assertEqual(updated.twitch_announce_interval_seconds, 900)
        await self.storage.mark_twitch_announcement_sent(giveaway.id, 12345)

        loaded = await self.storage.active_giveaway()
        self.assertTrue(loaded.twitch_announce_enabled)
        self.assertEqual(loaded.twitch_last_announce_at, 12345)

        enabled_again = await self.storage.configure_twitch_announcements(
            enabled=True, interval_minutes=15
        )
        self.assertEqual(enabled_again.twitch_last_announce_at, 12345)

        disabled = await self.storage.configure_twitch_announcements(enabled=False)
        self.assertFalse(disabled.twitch_announce_enabled)
        self.assertEqual(disabled.twitch_announce_interval_seconds, 900)
        self.assertIsNone(disabled.twitch_last_announce_at)

    async def test_twitch_announcement_interval_has_safe_bounds(self) -> None:
        await self.storage.start_giveaway(1, 1)

        for invalid in (0, 1441):
            with self.assertRaisesRegex(ValueError, "от 1 до 1440"):
                await self.storage.configure_twitch_announcements(
                    enabled=True, interval_minutes=invalid
                )

    async def test_latest_giveaway_by_title_returns_newest_casefold_match(self) -> None:
        first = await self.storage.start_giveaway(1, 1, 1, "Розыгрыш Ключей")
        await self.storage.finish_active_giveaway()
        second = await self.storage.start_giveaway(2, 1, 2, "розыгрыш ключей")

        found = await self.storage.latest_giveaway_by_title("РОЗЫГРЫШ КЛЮЧЕЙ")

        self.assertIsNotNone(found)
        self.assertEqual(found.id, second.id)
        self.assertEqual(found.winner_count, 2)
        self.assertNotEqual(found.id, first.id)
        self.assertIsNone(await self.storage.latest_giveaway_by_title("другой розыгрыш"))

    async def test_finish_closes_giveaway_without_eligible_participants(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 1, min_participants=2)

        finished = await self.storage.finish_active_giveaway(0)

        self.assertIsNotNone(finished)
        self.assertEqual(finished.id, giveaway.id)
        self.assertEqual(finished.state, "finished")
        self.assertEqual(finished.eligible_count_at_finish, 0)
        self.assertIsNone(await self.storage.active_giveaway())

    async def test_message_interval_filters_fast_messages(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 2, message_interval_seconds=30)
        code = await self.storage.create_link_code(101, "Alice", giveaway.id)
        await self.storage.claim_link_code(code, "alice_tv", "777")

        await self.storage.record_message("alice_tv", "777")
        await self.storage.record_message("alice_tv", "777")
        await self.storage._db.execute(
            """UPDATE giveaway_activity
               SET seconds = 60, last_counted_message_at = last_counted_message_at - 30
               WHERE giveaway_id = ? AND twitch_login = 'alice_tv'""",
            (giveaway.id,),
        )
        await self.storage._db.commit()
        await self.storage.record_message("alice_tv", "777")

        participants = await self.storage.giveaway_participants(giveaway)
        self.assertEqual(participants[0].messages, 2)

    async def test_excluded_login_is_not_listed_or_eligible(self) -> None:
        giveaway = await self.storage.start_giveaway(
            1,
            1,
            excluded_twitch_logins=("deadlock_otp",),
        )
        for telegram_id, login, twitch_id in (
            (101, "deadlock_otp", "777"),
            (102, "viewer_tv", "778"),
        ):
            code = await self.storage.create_link_code(telegram_id, login, giveaway.id)
            await self.storage.claim_link_code(code, login, twitch_id)
        await self.storage.record_message("deadlock_otp", "777")
        await self.storage.record_message("viewer_tv", "778")
        await self.storage._db.execute(
            "UPDATE giveaway_activity SET seconds = 60, presence_started_at = NULL WHERE giveaway_id = ?",
            (giveaway.id,),
        )
        await self.storage._db.commit()

        participants = await self.storage.giveaway_participants(
            giveaway, ("deadlock_otp",)
        )
        candidates = await self.storage.eligible_candidates(giveaway, ("deadlock_otp",))

        self.assertEqual([participant.twitch_login for participant in participants], ["viewer_tv"])
        self.assertEqual([candidate.twitch_login for candidate in candidates], ["viewer_tv"])

    async def test_excluded_telegram_username_is_not_listed_or_eligible(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 1)
        people = (
            (101, "Alice", "Blocked_TG", "alice_tv", "777"),
            (102, "Bob", "allowed_tg", "bob_tv", "778"),
        )
        for telegram_id, name, tg_username, twitch_login, twitch_id in people:
            code = await self.storage.create_link_code(
                telegram_id, name, giveaway.id, tg_username
            )
            await self.storage.claim_link_code(code, twitch_login, twitch_id)
            await self.storage.record_message(twitch_login, twitch_id)
        await self.storage._db.execute(
            """UPDATE giveaway_activity
               SET seconds = 60, presence_started_at = NULL
               WHERE giveaway_id = ?""",
            (giveaway.id,),
        )
        await self.storage._db.commit()

        participants = await self.storage.giveaway_participants(
            giveaway, (), ("@blocked_tg",)
        )
        candidates = await self.storage.eligible_candidates(
            giveaway, (), ("BLOCKED_TG",)
        )
        tracked, qualified = await self.storage.giveaway_status(
            giveaway, (), ("blocked_tg",)
        )

        self.assertEqual([participant.twitch_login for participant in participants], ["bob_tv"])
        self.assertEqual([candidate.twitch_login for candidate in candidates], ["bob_tv"])
        self.assertEqual((tracked, qualified), (1, 1))

    async def test_new_giveaway_requires_a_new_registration(self) -> None:
        first = await self.storage.start_giveaway(1, 1)
        code = await self.storage.create_link_code(101, "Alice", first.id)
        await self.storage.claim_link_code(code, "alice_tv", "777")
        await self.storage.record_message("alice_tv", "777")
        await self.storage._db.execute(
            "UPDATE giveaway_activity SET seconds = 60, presence_started_at = NULL WHERE giveaway_id = ?",
            (first.id,),
        )
        await self.storage._db.commit()
        self.assertEqual(len(await self.storage.eligible_candidates(first)), 1)

        await self.storage.finish_active_giveaway()
        second = await self.storage.start_giveaway(1, 1)
        await self.storage.record_message("alice_tv", "777")
        await self.storage._db.execute(
            "UPDATE giveaway_activity SET seconds = 60, presence_started_at = NULL WHERE giveaway_id = ?",
            (second.id,),
        )
        await self.storage._db.commit()

        self.assertEqual(await self.storage.eligible_candidates(second), [])
        self.assertIsNone(await self.storage.participant_stats(second, 101))

        code = await self.storage.create_link_code(101, "Alice", second.id)
        await self.storage.claim_link_code(code, "alice_tv", "777")
        self.assertEqual(len(await self.storage.eligible_candidates(second)), 1)
        stats = await self.storage.participant_stats(second, 101)
        self.assertIsNotNone(stats)
        self.assertEqual((stats.seconds, stats.messages), (60, 1))

    async def test_live_tracking_opens_and_closes_presence(self) -> None:
        await self.storage.mark_joined("viewer_tv", "778", count_time=False)
        giveaway = await self.storage.start_giveaway(1, 1, count_existing_presence=False)
        code = await self.storage.create_link_code(101, "Viewer", giveaway.id)
        await self.storage.claim_link_code(code, "viewer_tv", "778")

        await self.storage.begin_live_tracking()
        await self.storage._db.execute(
            """UPDATE giveaway_activity SET presence_started_at = presence_started_at - 60
               WHERE giveaway_id = ? AND twitch_login = 'viewer_tv'""",
            (giveaway.id,),
        )
        await self.storage._db.commit()
        await self.storage.end_live_tracking()

        participants = await self.storage.giveaway_participants(giveaway)
        self.assertEqual(participants[0].seconds, 60)

    async def test_reset_chat_session_discards_crash_gap_and_preserves_graceful_time(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 1)
        await self.storage.mark_joined("viewer_tv", "778")
        await self.storage._db.execute(
            """UPDATE giveaway_activity
               SET seconds = 10, presence_started_at = presence_started_at - 60
               WHERE giveaway_id = ? AND twitch_login = 'viewer_tv'""",
            (giveaway.id,),
        )
        await self.storage._db.commit()

        await self.storage.reset_chat_session(preserve_elapsed=False)

        row = await (
            await self.storage._db.execute(
                """SELECT seconds, presence_started_at FROM giveaway_activity
                   WHERE giveaway_id = ? AND twitch_login = 'viewer_tv'""",
                (giveaway.id,),
            )
        ).fetchone()
        presence_count = await (
            await self.storage._db.execute(
                "SELECT COUNT(*) AS total FROM chat_presence"
            )
        ).fetchone()
        self.assertEqual((row["seconds"], row["presence_started_at"]), (10, None))
        self.assertEqual(presence_count["total"], 0)

        await self.storage.mark_joined("viewer_tv", "778")
        await self.storage._db.execute(
            """UPDATE giveaway_activity
               SET presence_started_at = presence_started_at - 60
               WHERE giveaway_id = ? AND twitch_login = 'viewer_tv'""",
            (giveaway.id,),
        )
        await self.storage._db.commit()

        await self.storage.reset_chat_session(preserve_elapsed=True)

        row = await (
            await self.storage._db.execute(
                """SELECT seconds, presence_started_at FROM giveaway_activity
                   WHERE giveaway_id = ? AND twitch_login = 'viewer_tv'""",
                (giveaway.id,),
            )
        ).fetchone()
        self.assertGreaterEqual(row["seconds"], 70)
        self.assertIsNone(row["presence_started_at"])

    async def test_disconnect_reset_cannot_race_with_live_tracking_start(self) -> None:
        await self.storage.mark_joined("viewer_tv", "778", count_time=False)
        giveaway = await self.storage.start_giveaway(
            1, 1, count_existing_presence=False
        )
        open_started = asyncio.Event()
        allow_open = asyncio.Event()
        original_open_activity = self.storage._open_activity

        async def slow_open_activity(
            giveaway_id: int,
            twitch_login: str,
            twitch_user_id: str | None,
            now: int,
        ) -> None:
            open_started.set()
            await allow_open.wait()
            await original_open_activity(
                giveaway_id, twitch_login, twitch_user_id, now
            )

        self.storage._open_activity = slow_open_activity  # type: ignore[method-assign]
        begin_task = asyncio.create_task(self.storage.begin_live_tracking())
        await open_started.wait()
        reset_task = asyncio.create_task(
            self.storage.reset_chat_session(preserve_elapsed=True)
        )
        await asyncio.sleep(0)
        self.assertFalse(reset_task.done())

        allow_open.set()
        await asyncio.gather(begin_task, reset_task)

        row = await (
            await self.storage._db.execute(
                """SELECT presence_started_at FROM giveaway_activity
                   WHERE giveaway_id = ? AND twitch_login = 'viewer_tv'""",
                (giveaway.id,),
            )
        ).fetchone()
        presence_count = await (
            await self.storage._db.execute(
                "SELECT COUNT(*) AS total FROM chat_presence"
            )
        ).fetchone()
        self.assertIsNone(row["presence_started_at"])
        self.assertEqual(presence_count["total"], 0)


class IrcParserTests(unittest.TestCase):
    def test_tags_are_separated_from_message(self) -> None:
        tags, message = parse_tags("@user-id=42;display-name=Alice :alice!a@a PRIVMSG #x :hello")
        self.assertEqual(tags["user-id"], "42")
        self.assertEqual(message, ":alice!a@a PRIVMSG #x :hello")

    def test_twitch_chat_state_tracks_connection(self) -> None:
        state = TwitchChatState()

        state.mark_connected()
        self.assertTrue(state.connected)
        self.assertIsNotNone(state.last_connected_at)
        self.assertIsNone(state.last_error)

        state.mark_line_seen()
        self.assertIsNotNone(state.last_irc_at)

        state.mark_disconnected("network error")
        self.assertFalse(state.connected)
        self.assertEqual(state.last_error, "network error")

        state.mark_stream_live("Test stream", "2026-08-08T00:00:00Z")
        self.assertTrue(state.stream_live)
        self.assertTrue(state.stream_known)
        self.assertEqual(state.stream_title, "Test stream")

        state.mark_stream_offline()
        self.assertFalse(state.stream_live)
        self.assertTrue(state.stream_known)


if __name__ == "__main__":
    unittest.main()
