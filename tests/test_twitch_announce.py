from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.storage import Storage
from app.twitch_announce import (
    TwitchGiveawayAnnouncer,
    giveaway_twitch_chat_announcement,
)
from app.twitch_chat import TwitchChatState


class TwitchGiveawayAnnouncerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temporary_directory.name) / "bot.sqlite3")
        await self.storage.connect()

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self.temporary_directory.cleanup()

    async def test_due_announcement_is_sent_once_per_configured_interval(self) -> None:
        giveaway = await self.storage.start_giveaway(
            10, 2, title="Тест", prize="Steam key"
        )
        state = TwitchChatState()
        state.mark_connected()
        state.mark_stream_live("Live", "now")
        sent: list[str] = []

        async def send_chat_message(text: str) -> bool:
            sent.append(text)
            return True

        announcer = TwitchGiveawayAnnouncer(
            storage=self.storage,
            twitch_state=state,
            send_chat_message=send_chat_message,
            bot_username="deadlock_otp_bot",
        )
        await announcer.configure(True, 15)

        self.assertTrue(await announcer.announce_if_due(1000))
        restarted_announcer = TwitchGiveawayAnnouncer(
            storage=self.storage,
            twitch_state=state,
            send_chat_message=send_chat_message,
            bot_username="deadlock_otp_bot",
        )
        self.assertFalse(await restarted_announcer.announce_if_due(1899))
        self.assertTrue(await restarted_announcer.announce_if_due(1900))

        self.assertEqual(len(sent), 2)
        self.assertIn("https://t.me/deadlock_otp_bot?start=link", sent[0])
        self.assertIn("Steam key", sent[0])
        loaded = await self.storage.active_giveaway()
        self.assertEqual(loaded.id, giveaway.id)
        self.assertEqual(loaded.twitch_last_announce_at, 1900)

    async def test_offline_or_disconnected_state_pauses_announcements(self) -> None:
        await self.storage.start_giveaway(1, 1)
        state = TwitchChatState()
        sent: list[str] = []

        async def send_chat_message(text: str) -> bool:
            sent.append(text)
            return True

        announcer = TwitchGiveawayAnnouncer(
            storage=self.storage,
            twitch_state=state,
            send_chat_message=send_chat_message,
            bot_username="deadlock_otp_bot",
        )
        await announcer.configure(True, 15)

        self.assertFalse(await announcer.announce_if_due(1000))
        state.mark_stream_live("Live", "now")
        self.assertFalse(await announcer.announce_if_due(1000))
        state.mark_connected()
        self.assertTrue(await announcer.announce_if_due(1000))
        self.assertEqual(len(sent), 1)

    async def test_stream_status_error_pauses_announcements_until_live_is_confirmed(self) -> None:
        await self.storage.start_giveaway(1, 1)
        state = TwitchChatState()
        state.mark_connected()
        state.mark_stream_live("Live", "now")
        state.mark_stream_error("Helix unavailable")
        sent: list[str] = []

        async def send_chat_message(text: str) -> bool:
            sent.append(text)
            return True

        announcer = TwitchGiveawayAnnouncer(
            storage=self.storage,
            twitch_state=state,
            send_chat_message=send_chat_message,
            bot_username="deadlock_otp_bot",
        )
        await announcer.configure(True, 15)

        self.assertFalse(await announcer.announce_if_due(1000))
        state.mark_stream_live("Live", "now")
        self.assertTrue(await announcer.announce_if_due(1001))
        self.assertEqual(len(sent), 1)

    async def test_failed_send_uses_backoff_and_does_not_advance_schedule(self) -> None:
        await self.storage.start_giveaway(1, 1)
        state = TwitchChatState()
        state.mark_stream_live("Live", "now")
        state.mark_connected()
        results = iter((False, True))
        calls = 0

        async def send_chat_message(_text: str) -> bool:
            nonlocal calls
            calls += 1
            return next(results)

        announcer = TwitchGiveawayAnnouncer(
            storage=self.storage,
            twitch_state=state,
            send_chat_message=send_chat_message,
            bot_username="deadlock_otp_bot",
            failure_retry_seconds=60,
        )
        await announcer.configure(True, 15)

        self.assertFalse(await announcer.announce_if_due(2000))
        self.assertFalse(await announcer.announce_if_due(2059))
        self.assertTrue(await announcer.announce_if_due(2060))

        self.assertEqual(calls, 2)
        loaded = await self.storage.active_giveaway()
        self.assertEqual(loaded.twitch_last_announce_at, 2060)

    async def test_database_error_after_send_cannot_spam_chat(self) -> None:
        await self.storage.start_giveaway(1, 1)
        state = TwitchChatState()
        state.mark_stream_live("Live", "now")
        state.mark_connected()
        sent: list[str] = []

        async def send_chat_message(text: str) -> bool:
            sent.append(text)
            return True

        original_mark_sent = self.storage.mark_twitch_announcement_sent
        persistence_calls = 0

        async def flaky_mark_sent(giveaway_id: int, sent_at: int | None = None) -> None:
            nonlocal persistence_calls
            persistence_calls += 1
            if persistence_calls == 1:
                raise RuntimeError("database busy")
            await original_mark_sent(giveaway_id, sent_at)

        self.storage.mark_twitch_announcement_sent = flaky_mark_sent
        announcer = TwitchGiveawayAnnouncer(
            storage=self.storage,
            twitch_state=state,
            send_chat_message=send_chat_message,
            bot_username="deadlock_otp_bot",
            failure_retry_seconds=60,
        )
        await announcer.configure(True, 15)

        self.assertTrue(await announcer.announce_if_due(1000))
        self.assertFalse(await announcer.announce_if_due(1005))
        self.assertFalse(await announcer.announce_if_due(1059))
        self.assertFalse(await announcer.announce_if_due(1060))

        self.assertEqual(len(sent), 1)
        self.assertEqual(persistence_calls, 2)
        loaded = await self.storage.active_giveaway()
        self.assertEqual(loaded.twitch_last_announce_at, 1000)

    async def test_enabling_an_already_enabled_announcer_keeps_schedule(self) -> None:
        await self.storage.start_giveaway(1, 1)
        state = TwitchChatState()
        state.mark_stream_live("Live", "now")
        state.mark_connected()
        sent: list[str] = []

        async def send_chat_message(text: str) -> bool:
            sent.append(text)
            return True

        announcer = TwitchGiveawayAnnouncer(
            storage=self.storage,
            twitch_state=state,
            send_chat_message=send_chat_message,
            bot_username="deadlock_otp_bot",
        )
        await announcer.configure(True, 15)
        self.assertTrue(await announcer.announce_if_due(1000))

        await announcer.configure(True, 15)
        self.assertFalse(await announcer.announce_if_due(1001))
        self.assertEqual(len(sent), 1)

    async def test_disabling_stops_due_announcements(self) -> None:
        giveaway = await self.storage.start_giveaway(1, 1)
        state = TwitchChatState()
        state.mark_stream_live("Live", "now")
        state.mark_connected()

        async def send_chat_message(_text: str) -> bool:
            raise AssertionError("disabled announcer must not send")

        announcer = TwitchGiveawayAnnouncer(
            storage=self.storage,
            twitch_state=state,
            send_chat_message=send_chat_message,
            bot_username="deadlock_otp_bot",
        )
        await announcer.configure(True, 15)
        await announcer.configure(False)

        self.assertFalse(await announcer.announce_if_due(1000))
        loaded = await self.storage.active_giveaway()
        self.assertEqual(loaded.id, giveaway.id)
        self.assertFalse(loaded.twitch_announce_enabled)


class TwitchAnnouncementTextTests(unittest.TestCase):
    def test_announcement_contains_registration_link_and_conditions(self) -> None:
        from app.storage import Giveaway

        giveaway = Giveaway(
            1,
            "active",
            "Розыгрыш ключа",
            "Steam key",
            1,
            1,
            600,
            2,
            30,
            1,
            None,
            None,
        )

        text = giveaway_twitch_chat_announcement(
            giveaway, "deadlock_otp_bot", announced_at=1000
        )

        self.assertIn("https://t.me/deadlock_otp_bot?start=link", text)
        self.assertIn("Розыгрыш ключа", text)
        self.assertIn("Steam key", text)
        self.assertIn("10 мин", text)
        self.assertIn("2 сообщений", text)


if __name__ == "__main__":
    unittest.main()
