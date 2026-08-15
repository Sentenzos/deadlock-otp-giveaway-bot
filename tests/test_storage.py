from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.main import pick_winners
from app.storage import DRAW_SCORE_MAX, DrawEligibility, Storage
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

    async def test_every_active_giveaway_parameter_can_be_updated_atomically(self) -> None:
        giveaway = await self.storage.start_giveaway(
            10,
            2,
            winner_count=1,
            title="Исходный",
            prize="Старый приз",
            message_interval_seconds=30,
            min_participants=1,
        )
        code = await self.storage.create_link_code(101, "Alice", giveaway.id, "alice_tg")
        await self.storage.configure_twitch_announcements(enabled=True, interval_minutes=15)
        await self.storage.mark_twitch_announcement_sent(giveaway.id, 12345)
        end_at = self.storage.now() + 3600

        updated = await self.storage.update_active_giveaway(
            min_minutes=100,
            min_messages=12,
            winner_count=3,
            message_interval_seconds=45,
            min_participants=7,
            end_at=end_at,
            title="  Новое название  ",
            prize="  Steam 2000 ₽  ",
        )

        self.assertIsNotNone(updated)
        self.assertEqual(
            (
                updated.id,
                updated.started_at,
                updated.min_seconds,
                updated.min_messages,
                updated.winner_count,
                updated.message_interval_seconds,
                updated.min_participants,
                updated.end_at,
                updated.title,
                updated.prize,
            ),
            (
                giveaway.id,
                giveaway.started_at,
                6000,
                12,
                3,
                45,
                7,
                end_at,
                "Новое название",
                "Steam 2000 ₽",
            ),
        )
        self.assertTrue(updated.twitch_announce_enabled)
        self.assertEqual(updated.twitch_announce_interval_seconds, 900)
        self.assertEqual(updated.twitch_last_announce_at, 12345)
        self.assertIsNotNone(await self.storage.latest_giveaway_by_title("НОВОЕ НАЗВАНИЕ"))
        self.assertIsNone(await self.storage.latest_giveaway_by_title("Исходный"))

        status, telegram_id, _ = await self.storage.claim_link_code(
            code, "alice_tv", "777"
        )
        self.assertEqual((status, telegram_id), ("linked", 101))
        self.assertIsNotNone(await self.storage.participant_stats(updated, 101))

        cleared = await self.storage.update_active_giveaway(prize="", end_at=None)
        self.assertEqual(cleared.prize, "")
        self.assertIsNone(cleared.end_at)

    async def test_update_thresholds_reuses_accumulated_activity(self) -> None:
        giveaway = await self.storage.start_giveaway(10, 5)
        code = await self.storage.create_link_code(101, "Alice", giveaway.id)
        await self.storage.claim_link_code(code, "alice_tv", "777")
        await self.storage._db.execute(
            """INSERT INTO giveaway_activity
               (giveaway_id, twitch_login, twitch_user_id, seconds, messages,
                presence_started_at)
               VALUES (?, 'alice_tv', '777', 300, 2, NULL)
               ON CONFLICT(giveaway_id, twitch_login) DO UPDATE SET
                   seconds = 300, messages = 2, presence_started_at = NULL""",
            (giveaway.id,),
        )
        await self.storage._db.commit()

        lowered = await self.storage.update_active_giveaway(
            min_minutes=5, min_messages=2
        )
        self.assertEqual(
            [candidate.telegram_user_id for candidate in await self.storage.eligible_candidates(lowered)],
            [101],
        )

        raised = await self.storage.update_active_giveaway(min_minutes=6)
        self.assertEqual(await self.storage.eligible_candidates(raised), [])
        row = await (
            await self.storage._db.execute(
                """SELECT seconds, messages FROM giveaway_activity
                   WHERE giveaway_id = ? AND twitch_login = 'alice_tv'""",
                (giveaway.id,),
            )
        ).fetchone()
        self.assertEqual((row["seconds"], row["messages"]), (300, 2))

    async def test_updated_message_interval_applies_only_to_future_messages(self) -> None:
        giveaway = await self.storage.start_giveaway(
            1, 2, message_interval_seconds=3600
        )
        code = await self.storage.create_link_code(101, "Alice", giveaway.id)
        await self.storage.claim_link_code(code, "alice_tv", "777")
        await self.storage.record_message("alice_tv", "777")

        updated = await self.storage.update_active_giveaway(
            message_interval_seconds=0
        )
        await self.storage.record_message("alice_tv", "777")

        stats = await self.storage.participant_stats(updated, 101)
        self.assertEqual(stats.messages, 2)

    async def test_invalid_giveaway_updates_change_nothing(self) -> None:
        original = await self.storage.start_giveaway(
            10,
            2,
            winner_count=1,
            title="Неизменный",
            prize="Приз",
            message_interval_seconds=30,
            min_participants=1,
        )
        invalid_updates = (
            {"min_minutes": 0},
            {"min_messages": 0},
            {"winner_count": 101},
            {"message_interval_seconds": -1},
            {"min_participants": 100_001},
            {"end_at": self.storage.now() - 1},
            {"title": " "},
            {"title": "я" * 121},
            {"prize": "п" * 301},
            {"min_minutes": int("9" * 100)},
            {"min_messages": int("9" * 100)},
            {"message_interval_seconds": int("9" * 100)},
        )

        for kwargs in invalid_updates:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    await self.storage.update_active_giveaway(**kwargs)
                self.assertEqual(await self.storage.active_giveaway(), original)

        with self.assertRaisesRegex(ValueError, "Не указаны параметры"):
            await self.storage.update_active_giveaway()
        await self.storage.finish_active_giveaway()
        self.assertIsNone(await self.storage.update_active_giveaway(min_minutes=20))

    async def test_finish_refuses_stale_giveaway_rules(self) -> None:
        original = await self.storage.start_giveaway(10, 2, title="До изменения")
        updated = await self.storage.update_active_giveaway(
            min_minutes=20, title="После изменения"
        )

        stale_finish = await self.storage.finish_active_giveaway(
            0, expected_giveaway=original
        )

        self.assertIsNone(stale_finish)
        self.assertEqual(await self.storage.active_giveaway(), updated)
        finished = await self.storage.finish_active_giveaway(
            0, expected_giveaway=updated
        )
        self.assertEqual(finished.state, "finished")
        self.assertEqual((finished.title, finished.min_seconds), ("После изменения", 1200))

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

    async def test_draw_preview_contains_every_registration_and_technical_flags(self) -> None:
        giveaway = await self.storage.start_giveaway(2, 3)
        people = (
            (101, "Alice", "alice_tg", "alice_tv", "701"),
            (102, "Bob", "blocked_tg", "bob_tv", "702"),
            (103, "Cara", "cara_tg", "ignored_tv", "703"),
        )
        for telegram_id, name, username, login, twitch_id in people:
            code = await self.storage.create_link_code(
                telegram_id, name, giveaway.id, username
            )
            await self.storage.claim_link_code(code, login, twitch_id)
        await self.storage._db.executemany(
            """INSERT INTO giveaway_activity
               (giveaway_id, twitch_login, twitch_user_id, seconds, messages)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(giveaway_id, twitch_login) DO UPDATE SET
                   seconds = excluded.seconds, messages = excluded.messages,
                   presence_started_at = NULL""",
            (
                (giveaway.id, "alice_tv", "701", 120, 3),
                (giveaway.id, "ignored_tv", "703", 120, 3),
            ),
        )
        await self.storage._db.commit()

        preview = await self.storage.draw_participants(
            giveaway,
            excluded_twitch_logins=("@IGNORED_TV",),
            excluded_telegram_usernames=("BLOCKED_TG",),
        )
        by_id = {participant.telegram_user_id: participant for participant in preview}

        self.assertEqual(set(by_id), {101, 102, 103})
        self.assertEqual(
            (by_id[101].seconds, by_id[101].messages, by_id[101].twitch_user_id),
            (120, 3, "701"),
        )
        self.assertEqual((by_id[102].seconds, by_id[102].messages), (0, 0))
        self.assertFalse(by_id[102].time_requirement_met)
        self.assertFalse(by_id[102].message_requirement_met)
        self.assertTrue(by_id[102].excluded_by_telegram)
        self.assertIn("not_enough_time", by_id[102].eligibility_reason)
        self.assertTrue(by_id[103].excluded_by_twitch)
        self.assertEqual(by_id[103].telegram_username, "cara_tg")
        self.assertGreater(by_id[103].registered_at, 0)

    async def test_finish_draw_snapshots_all_scores_and_chooses_top_eligible(self) -> None:
        giveaway = await self.storage.start_giveaway(
            1, 1, winner_count=1, min_participants=2, title="Audit draw"
        )
        people = (
            (101, "Alice", "alice_tv", "701"),
            (102, "Bob", "bob_tv", "702"),
            (103, "Cara", "cara_tv", "703"),
            (104, "Dan", "dan_tv", "704"),
        )
        for telegram_id, name, login, twitch_id in people:
            code = await self.storage.create_link_code(
                telegram_id, name, giveaway.id, f"{login}_tg"
            )
            await self.storage.claim_link_code(code, login, twitch_id)
        await self.storage._db.executemany(
            """INSERT INTO giveaway_activity
               (giveaway_id, twitch_login, twitch_user_id, seconds, messages,
                presence_started_at)
               VALUES (?, ?, ?, ?, 1, NULL)
               ON CONFLICT(giveaway_id, twitch_login) DO UPDATE SET
                   seconds = excluded.seconds, messages = 1,
                   presence_started_at = NULL""",
            (
                (giveaway.id, "alice_tv", "701", 60),
                (giveaway.id, "bob_tv", "702", 60),
                (giveaway.id, "cara_tv", "703", 59),
                (giveaway.id, "dan_tv", "704", 60),
            ),
        )
        await self.storage._db.commit()
        score_values = iter((10, 99, 99, 90, 80))

        result = await self.storage.create_draw_round(
            giveaway,
            {
                101: True,
                102: DrawEligibility(False, "telegram_check_error"),
                103: True,
                104: True,
            },
            score_factory=lambda: next(score_values),
        )
        entries = {entry.telegram_user_id: entry for entry in result.entries}

        self.assertEqual(result.round.round_number, 1)
        self.assertEqual(result.round.round_kind, "finish")
        self.assertEqual(result.round.registered_count, 4)
        self.assertEqual(result.round.eligible_count, 2)
        self.assertTrue(result.round.min_participants_met)
        self.assertEqual(len(entries), 4)
        self.assertEqual({entry.random_score for entry in entries.values()}, {"10", "99", "90", "80"})
        self.assertTrue(all(isinstance(entry.random_score, str) for entry in entries.values()))
        self.assertEqual([winner.telegram_user_id for winner in result.winners], [104])
        self.assertEqual(entries[104].draw_rank, 1)
        self.assertEqual(entries[101].draw_rank, 2)
        self.assertFalse(entries[102].final_eligible)
        self.assertEqual(entries[102].eligibility_reason, "telegram_check_error")
        self.assertFalse(entries[103].final_eligible)
        self.assertEqual(entries[103].eligibility_reason, "not_enough_time")
        self.assertIsNone(entries[102].draw_rank)
        self.assertIsNone(await self.storage.active_giveaway())
        self.assertEqual(
            [winner.telegram_user_id for winner in await self.storage.recorded_winners(giveaway)],
            [104],
        )

    async def test_random_scores_are_unique_and_fit_the_public_twelve_digit_range(self) -> None:
        with patch(
            "app.storage.secrets.randbelow",
            side_effect=(7, 7, DRAW_SCORE_MAX),
        ) as random_value:
            scores = Storage._unique_draw_scores(2, None)

        self.assertEqual(scores, [7, DRAW_SCORE_MAX])
        random_value.assert_called_with(DRAW_SCORE_MAX + 1)
        with self.assertRaisesRegex(ValueError, "не более 12 цифр"):
            Storage._unique_draw_scores(
                1,
                lambda: DRAW_SCORE_MAX + 1,
            )

    async def test_normal_below_minimum_has_no_winners_but_force_ignores_only_minimum(self) -> None:
        async def register_two(giveaway_id: int) -> dict[int, bool]:
            for telegram_id, login in ((101, "alice_tv"), (102, "bob_tv")):
                code = await self.storage.create_link_code(
                    telegram_id, login, giveaway_id
                )
                await self.storage.claim_link_code(code, login, str(telegram_id))
                await self.storage._db.execute(
                    """INSERT INTO giveaway_activity
                       (giveaway_id, twitch_login, twitch_user_id, seconds, messages)
                       VALUES (?, ?, ?, 60, 1)
                       ON CONFLICT(giveaway_id, twitch_login) DO UPDATE SET
                           seconds = 60, messages = 1, presence_started_at = NULL""",
                    (giveaway_id, login, str(telegram_id)),
                )
            await self.storage._db.commit()
            return {101: True, 102: True}

        normal = await self.storage.start_giveaway(1, 1, min_participants=3)
        normal_result = await self.storage.create_draw_round(
            normal,
            await register_two(normal.id),
            score_factory=iter((10, 20)).__next__,
        )
        self.assertFalse(normal_result.round.min_participants_met)
        self.assertFalse(normal_result.round.forced)
        self.assertEqual(normal_result.winners, ())
        self.assertTrue(all(entry.random_score for entry in normal_result.entries))

        forced = await self.storage.start_giveaway(1, 1, min_participants=3)
        forced_result = await self.storage.create_draw_round(
            forced,
            await register_two(forced.id),
            force=True,
            score_factory=iter((10, 20)).__next__,
        )
        self.assertFalse(forced_result.round.min_participants_met)
        self.assertTrue(forced_result.round.forced)
        self.assertEqual([winner.telegram_user_id for winner in forced_result.winners], [102])

        ineligible = await self.storage.start_giveaway(2, 2, min_participants=3)
        decisions = await register_two(ineligible.id)
        forced_ineligible = await self.storage.create_draw_round(
            ineligible,
            decisions,
            force=True,
            score_factory=iter((30, 40)).__next__,
        )
        self.assertEqual(forced_ineligible.round.eligible_count, 0)
        self.assertEqual(forced_ineligible.winners, ())

    async def test_reroll_cannot_bypass_an_unmet_minimum_after_no_winner_finish(self) -> None:
        giveaway = await self.storage.start_giveaway(
            1,
            1,
            min_participants=2,
        )
        code = await self.storage.create_link_code(101, "Alice", giveaway.id)
        await self.storage.claim_link_code(code, "alice_tv", "701")
        await self.storage._db.execute(
            """INSERT INTO giveaway_activity
               (giveaway_id, twitch_login, twitch_user_id, seconds, messages)
               VALUES (?, 'alice_tv', '701', 60, 1)
               ON CONFLICT(giveaway_id, twitch_login) DO UPDATE SET
                   seconds = 60, messages = 1, presence_started_at = NULL""",
            (giveaway.id,),
        )
        await self.storage._db.commit()

        finished = await self.storage.create_draw_round(
            giveaway,
            {101: True},
            score_factory=lambda: 10,
        )
        self.assertFalse(finished.round.min_participants_met)
        self.assertEqual(finished.winners, ())

        with self.assertRaisesRegex(ValueError, "только после выбора"):
            await self.storage.create_draw_round(
                giveaway,
                {101: True},
                reroll=True,
                score_factory=lambda: 20,
            )
        self.assertEqual(len(await self.storage.draw_rounds(giveaway)), 1)
        self.assertEqual(await self.storage.recorded_winners(giveaway), [])

    async def test_initial_draw_is_idempotent_and_reroll_is_a_new_round(self) -> None:
        giveaway = await self.storage.start_giveaway(
            1, 1, winner_count=2, min_participants=1
        )
        decisions: dict[int, bool] = {}
        for telegram_id, login in (
            (101, "alice_tv"),
            (102, "bob_tv"),
            (103, "cara_tv"),
        ):
            code = await self.storage.create_link_code(
                telegram_id, login, giveaway.id
            )
            await self.storage.claim_link_code(code, login, str(telegram_id))
            await self.storage._db.execute(
                """INSERT INTO giveaway_activity
                   (giveaway_id, twitch_login, twitch_user_id, seconds, messages)
                   VALUES (?, ?, ?, 60, 1)
                   ON CONFLICT(giveaway_id, twitch_login) DO UPDATE SET
                       seconds = 60, messages = 1, presence_started_at = NULL""",
                (giveaway.id, login, str(telegram_id)),
            )
            decisions[telegram_id] = True
        await self.storage._db.commit()

        first = await self.storage.create_draw_round(
            giveaway,
            decisions,
            score_factory=iter((90, 80, 70)).__next__,
        )
        first_entries = first.entries
        repeated = await self.storage.create_draw_round(
            giveaway,
            decisions,
            score_factory=lambda: (_ for _ in ()).throw(AssertionError("RNG called")),
        )

        self.assertEqual(repeated, first)
        self.assertEqual([winner.telegram_user_id for winner in first.winners], [101, 102])
        rerolled = await self.storage.create_draw_round(
            giveaway,
            decisions,
            reroll=True,
            score_factory=iter((100, 99, 1)).__next__,
        )
        reroll_entries = {entry.telegram_user_id: entry for entry in rerolled.entries}
        self.assertEqual(rerolled.round.round_number, 2)
        self.assertEqual(rerolled.round.round_kind, "reroll")
        self.assertEqual(rerolled.round.requested_winner_count, 1)
        self.assertEqual([winner.telegram_user_id for winner in rerolled.winners], [103])
        self.assertTrue(reroll_entries[101].previous_winner)
        self.assertTrue(reroll_entries[102].previous_winner)
        self.assertFalse(reroll_entries[101].final_eligible)
        self.assertEqual(reroll_entries[103].draw_rank, 1)
        self.assertEqual((await self.storage.draw_rounds(giveaway))[0].round_number, 1)
        self.assertEqual((await self.storage.draw_entries(first.round.id)), list(first_entries))
        self.assertEqual((await self.storage.latest_draw_result(giveaway)), rerolled)
        self.assertEqual(
            [winner.telegram_user_id for winner in await self.storage.recorded_winners(giveaway)],
            [101, 102, 103],
        )
        with self.assertRaisesRegex(ValueError, "нет допущенных участников"):
            await self.storage.create_draw_round(
                giveaway,
                decisions,
                reroll=True,
                score_factory=iter((3, 2, 1)).__next__,
            )
        self.assertEqual(len(await self.storage.draw_rounds(giveaway)), 2)

    async def test_draw_refuses_stale_rules_and_changed_registration_set(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 1)
        code = await self.storage.create_link_code(101, "Alice", giveaway.id)
        await self.storage.claim_link_code(code, "alice_tv", "701")
        await self.storage.mark_joined("alice_tv", "701")
        stale_decisions = {101: True}
        updated = await self.storage.update_active_giveaway(min_minutes=2)

        with self.assertRaisesRegex(ValueError, "Параметры розыгрыша изменились"):
            await self.storage.create_draw_round(giveaway, stale_decisions)

        row = await (
            await self.storage._db.execute(
                """SELECT presence_started_at FROM giveaway_activity
                   WHERE giveaway_id = ? AND twitch_login = 'alice_tv'""",
                (giveaway.id,),
            )
        ).fetchone()
        self.assertIsNotNone(row["presence_started_at"])
        self.assertEqual(await self.storage.active_giveaway(), updated)
        self.assertEqual(await self.storage.draw_rounds(giveaway), [])

        code = await self.storage.create_link_code(102, "Bob", giveaway.id)
        await self.storage.claim_link_code(code, "bob_tv", "702")
        with self.assertRaisesRegex(ValueError, "Список регистраций изменился"):
            await self.storage.create_draw_round(updated, stale_decisions)
        self.assertEqual(await self.storage.draw_rounds(giveaway), [])
        self.assertIsNotNone(await self.storage.active_giveaway())

    async def test_draw_transaction_rolls_back_entries_winners_finish_and_presence(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 1)
        code = await self.storage.create_link_code(101, "Alice", giveaway.id)
        await self.storage.claim_link_code(code, "alice_tv", "701")
        await self.storage.mark_joined("alice_tv", "701")
        await self.storage._db.execute(
            """UPDATE giveaway_activity SET seconds = 60, messages = 1
               WHERE giveaway_id = ? AND twitch_login = 'alice_tv'""",
            (giveaway.id,),
        )
        await self.storage._db.execute(
            """CREATE TRIGGER fail_atomic_draw_winner
               BEFORE INSERT ON giveaway_winners
               BEGIN SELECT RAISE(ABORT, 'test winner failure'); END"""
        )
        await self.storage._db.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            await self.storage.create_draw_round(
                giveaway, {101: True}, score_factory=lambda: DRAW_SCORE_MAX
            )

        counts = {}
        for table in ("giveaway_draw_rounds", "giveaway_draw_entries", "giveaway_winners"):
            row = await (
                await self.storage._db.execute(f"SELECT COUNT(*) AS total FROM {table}")
            ).fetchone()
            counts[table] = int(row["total"])
        activity = await (
            await self.storage._db.execute(
                """SELECT seconds, messages, presence_started_at
                   FROM giveaway_activity
                   WHERE giveaway_id = ? AND twitch_login = 'alice_tv'""",
                (giveaway.id,),
            )
        ).fetchone()
        self.assertEqual(counts, {
            "giveaway_draw_rounds": 0,
            "giveaway_draw_entries": 0,
            "giveaway_winners": 0,
        })
        self.assertEqual((activity["seconds"], activity["messages"]), (60, 1))
        self.assertIsNotNone(activity["presence_started_at"])
        self.assertIsNotNone(await self.storage.active_giveaway())

    async def test_concurrent_message_cannot_commit_a_partial_draw_transaction(self) -> None:
        frozen_clock = patch("app.storage.time.time", return_value=1_900_000_000)
        frozen_clock.start()
        self.addCleanup(frozen_clock.stop)
        giveaway = await self.storage.start_giveaway(1, 1)
        code = await self.storage.create_link_code(101, "Alice", giveaway.id)
        await self.storage.claim_link_code(code, "alice_tv", "701")
        await self.storage.mark_joined("alice_tv", "701")
        await self.storage._db.execute(
            """UPDATE giveaway_activity SET seconds = 60, messages = 1
               WHERE giveaway_id = ? AND twitch_login = 'alice_tv'""",
            (giveaway.id,),
        )
        await self.storage._db.commit()

        draw_has_write_lock = asyncio.Event()
        allow_draw_to_continue = asyncio.Event()
        original_snapshot = self.storage._draw_participants_at

        async def paused_snapshot(*args: object, **kwargs: object):
            result = await original_snapshot(*args, **kwargs)
            if kwargs.get("db") is not None:
                draw_has_write_lock.set()
                await allow_draw_to_continue.wait()
            return result

        self.storage._draw_participants_at = paused_snapshot  # type: ignore[method-assign]
        try:
            draw_task = asyncio.create_task(
                self.storage.create_draw_round(
                    giveaway, {101: True}, score_factory=lambda: 42
                )
            )
            await draw_has_write_lock.wait()
            invisible_rounds = await (
                await self.storage._db.execute(
                    "SELECT COUNT(*) AS total FROM giveaway_draw_rounds"
                )
            ).fetchone()
            self.assertEqual(invisible_rounds["total"], 0)
            message_task = asyncio.create_task(
                self.storage.record_message("alice_tv", "701")
            )
            await asyncio.sleep(0.05)

            self.assertFalse(message_task.done())

            allow_draw_to_continue.set()
            result = await draw_task
            await message_task
        finally:
            self.storage._draw_participants_at = original_snapshot  # type: ignore[method-assign]

        self.assertEqual([winner.telegram_user_id for winner in result.winners], [101])
        self.assertIsNone(await self.storage.active_giveaway())
        entry = (await self.storage.draw_entries(result.round.id))[0]
        self.assertEqual((entry.seconds, entry.messages), (60, 1))
        winner_count = await (
            await self.storage._db.execute(
                "SELECT COUNT(*) AS total FROM giveaway_winners"
            )
        ).fetchone()
        self.assertEqual(winner_count["total"], 1)

    async def test_link_claim_cannot_register_after_draw_snapshot_is_committed(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 1)
        code = await self.storage.create_link_code(101, "Alice", giveaway.id)
        racing_storage = Storage(self.storage.path)
        await racing_storage.connect()
        draw_has_write_lock = asyncio.Event()
        allow_draw_to_continue = asyncio.Event()
        original_snapshot = self.storage._draw_participants_at

        async def paused_snapshot(*args: object, **kwargs: object):
            result = await original_snapshot(*args, **kwargs)
            if kwargs.get("db") is not None:
                draw_has_write_lock.set()
                await allow_draw_to_continue.wait()
            return result

        self.storage._draw_participants_at = paused_snapshot  # type: ignore[method-assign]
        try:
            draw_task = asyncio.create_task(
                self.storage.create_draw_round(
                    giveaway,
                    {},
                    score_factory=lambda: 42,
                )
            )
            await draw_has_write_lock.wait()
            claim_task = asyncio.create_task(
                racing_storage.claim_link_code(code, "alice_tv", "701")
            )
            await asyncio.sleep(0.05)
            self.assertFalse(claim_task.done())

            allow_draw_to_continue.set()
            draw_result = await draw_task
            claim_result = await claim_task
        finally:
            self.storage._draw_participants_at = original_snapshot  # type: ignore[method-assign]
            await racing_storage.close()

        self.assertEqual(draw_result.round.registered_count, 0)
        self.assertEqual(claim_result, ("giveaway_closed", None, None))
        registration_count = await (
            await self.storage._db.execute(
                """SELECT COUNT(*) AS total FROM giveaway_registrations
                   WHERE giveaway_id = ?""",
                (giveaway.id,),
            )
        ).fetchone()
        self.assertEqual(registration_count["total"], 0)

    async def test_additive_draw_migration_preserves_active_giveaway_progress_exactly(self) -> None:
        database_path = self.storage.path
        giveaway = await self.storage.start_giveaway(
            5,
            2,
            winner_count=2,
            title="Живой розыгрыш",
            prize="Приз",
            message_interval_seconds=17,
            min_participants=4,
        )
        registration_code = await self.storage.create_link_code(
            101, "Alice", giveaway.id, "alice_tg"
        )
        await self.storage.claim_link_code(registration_code, "alice_tv", "701")
        pending_code = await self.storage.create_link_code(
            202, "Pending", giveaway.id, "pending_tg"
        )
        await self.storage.mark_joined("alice_tv", "701")
        await self.storage._db.execute(
            """UPDATE giveaway_activity
               SET seconds = 321, messages = 7,
                   last_counted_message_at = 123456,
                   presence_started_at = 123400
               WHERE giveaway_id = ? AND twitch_login = 'alice_tv'""",
            (giveaway.id,),
        )
        await self.storage._db.execute(
            """UPDATE chat_presence SET joined_at = 123399
               WHERE twitch_login = 'alice_tv'"""
        )
        await self.storage._db.commit()

        protected_tables = (
            "giveaways",
            "giveaway_registrations",
            "giveaway_activity",
            "link_codes",
            "chat_presence",
            "twitch_links",
        )

        async def exact_rows(storage: Storage) -> dict[str, list[tuple[object, ...]]]:
            result: dict[str, list[tuple[object, ...]]] = {}
            for table in protected_tables:
                rows = await (
                    await storage._db.execute(f"SELECT * FROM {table} ORDER BY rowid")
                ).fetchall()
                result[table] = [tuple(row) for row in rows]
            return result

        before = await exact_rows(self.storage)
        self.assertEqual(before["link_codes"][0][0], pending_code)
        await self.storage.close()
        legacy_db = sqlite3.connect(database_path)
        try:
            legacy_db.execute("DROP TABLE giveaway_draw_entries")
            legacy_db.execute("DROP TABLE giveaway_draw_rounds")
            legacy_db.commit()
        finally:
            legacy_db.close()

        self.storage = Storage(database_path)
        await self.storage.connect()
        after = await exact_rows(self.storage)

        self.assertEqual(after, before)
        active = await self.storage.active_giveaway()
        self.assertIsNotNone(active)
        self.assertEqual(active.id, giveaway.id)
        activity = after["giveaway_activity"][0]
        self.assertIn(321, activity)
        self.assertIn(7, activity)
        self.assertIn(123400, activity)
        tables = await (
            await self.storage._db.execute(
                """SELECT name FROM sqlite_master
                   WHERE type = 'table' AND name LIKE 'giveaway_draw_%'
                   ORDER BY name"""
            )
        ).fetchall()
        self.assertEqual(
            [str(row["name"]) for row in tables],
            ["giveaway_draw_entries", "giveaway_draw_rounds"],
        )


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
