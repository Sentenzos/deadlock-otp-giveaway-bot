from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from app.twitch_chat import (
    TwitchChat,
    TwitchChatState,
    TwitchLiveMonitor,
    parse_link_code,
    sanitize_chat_message,
)


class FakeStorage:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str | None]] = []
        self.presence_events: list[tuple[str, str | None, bool, bool]] = []
        self.claims: list[tuple[str, str, str]] = []

    async def record_message(
        self,
        twitch_login: str,
        twitch_user_id: str | None,
        count_message: bool = True,
        count_time: bool = True,
    ) -> None:
        self.presence_events.append(
            (twitch_login, twitch_user_id, count_message, count_time)
        )
        if count_message:
            self.messages.append((twitch_login, twitch_user_id))

    async def claim_link_code(
        self, code: str, twitch_login: str, twitch_user_id: str
    ) -> tuple[str, int | None, str | None]:
        self.claims.append((code, twitch_login, twitch_user_id))
        return "linked", 101, "Alice"


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, line: str) -> None:
        self.sent.append(line)


class TwitchChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_monitor_can_refresh_immediately(self) -> None:
        async def on_live_changed(_is_live: bool) -> None:
            return None

        monitor = TwitchLiveMonitor(
            channel="deadlock_otp",
            oauth_token="token",
            state=TwitchChatState(),
            on_live_changed=on_live_changed,
            poll_interval_seconds=1,
        )
        check = AsyncMock()
        monitor._check_once = check

        await monitor.refresh()

        check.assert_awaited_once()
        self.assertEqual(monitor.poll_interval_seconds, 5)

    async def test_live_monitor_updates_online_and_offline_transitions(self) -> None:
        transitions: list[bool] = []

        async def on_live_changed(is_live: bool) -> None:
            transitions.append(is_live)

        state = TwitchChatState()
        monitor = TwitchLiveMonitor(
            channel="deadlock_otp",
            oauth_token="token",
            state=state,
            on_live_changed=on_live_changed,
            poll_interval_seconds=10,
        )
        monitor._client_id_for_token = AsyncMock(return_value="client-id")
        monitor._fetch_stream = AsyncMock(
            side_effect=[
                {"type": "live", "title": "Live test", "started_at": "now"},
                None,
            ]
        )

        await monitor.refresh()
        self.assertTrue(state.stream_live)
        self.assertEqual(state.stream_title, "Live test")
        await monitor.refresh()
        self.assertFalse(state.stream_live)
        self.assertTrue(state.stream_known)
        self.assertEqual(transitions, [True, False])

    async def test_live_monitor_reuses_status_checked_in_last_three_seconds(self) -> None:
        async def on_live_changed(_is_live: bool) -> None:
            return None

        state = TwitchChatState()
        state.mark_stream_offline()
        monitor = TwitchLiveMonitor(
            channel="deadlock_otp",
            oauth_token="token",
            state=state,
            on_live_changed=on_live_changed,
        )
        check = AsyncMock()
        monitor._check_once = check

        await monitor.refresh_if_stale()
        check.assert_not_awaited()
        state.last_stream_check_at -= 4
        await monitor.refresh_if_stale()
        check.assert_awaited_once()

    async def test_fetch_chatters_uses_moderator_scope_and_returns_logins(self) -> None:
        async def on_live_changed(_is_live: bool) -> None:
            return None

        monitor = TwitchLiveMonitor(
            channel="deadlock_otp",
            oauth_token="token",
            state=TwitchChatState(),
            on_live_changed=on_live_changed,
        )
        monitor._client_id_for_token = AsyncMock(return_value="client-id")
        monitor._token_user_id = "broadcaster-id"
        monitor._token_scopes = frozenset({"chat:read", "moderator:read:chatters"})
        monitor._broadcaster_id_for_channel = AsyncMock(return_value="broadcaster-id")
        monitor._fetch_chatters = AsyncMock(return_value=["alice_tv", "bob_tv"])

        chatters = await monitor.fetch_chatters()

        self.assertEqual(chatters, ["alice_tv", "bob_tv"])
        monitor._fetch_chatters.assert_awaited_once()

    async def test_fetch_chatters_explains_missing_scope(self) -> None:
        async def on_live_changed(_is_live: bool) -> None:
            return None

        monitor = TwitchLiveMonitor(
            channel="deadlock_otp",
            oauth_token="token",
            state=TwitchChatState(),
            on_live_changed=on_live_changed,
        )
        monitor._client_id_for_token = AsyncMock(return_value="client-id")
        monitor._token_user_id = "broadcaster-id"
        monitor._token_scopes = frozenset({"chat:read"})

        with self.assertRaisesRegex(RuntimeError, "moderator:read:chatters"):
            await monitor.fetch_chatters()

    async def test_twitch_chat_announcements_require_chat_edit_scope(self) -> None:
        async def on_live_changed(_is_live: bool) -> None:
            return None

        monitor = TwitchLiveMonitor(
            channel="deadlock_otp",
            oauth_token="token",
            state=TwitchChatState(),
            on_live_changed=on_live_changed,
        )
        monitor._client_id_for_token = AsyncMock(return_value="client-id")
        monitor._token_scopes = frozenset({"chat:read"})

        with self.assertRaisesRegex(RuntimeError, "chat:edit"):
            await monitor.require_chat_edit_scope()

        monitor._token_scopes = frozenset({"chat:read", "chat:edit"})
        await monitor.require_chat_edit_scope()

    async def test_excluded_login_can_link_but_messages_are_not_counted(self) -> None:
        storage = FakeStorage()
        linked: list[tuple[int, str]] = []

        async def notify_linked(telegram_user_id: int, name: str) -> None:
            linked.append((telegram_user_id, name))

        state = TwitchChatState()
        chat = TwitchChat(
            channel="deadlock_otp",
            bot_login="deadlock_otp",
            oauth_token="token",
            storage=storage,  # type: ignore[arg-type]
            notify_linked=notify_linked,
            state=state,
            tracking_enabled=lambda: True,
            excluded_logins=("deadlock_otp",),
        )

        await chat._handle_privmsg(
            {"user-id": "777"},
            ":deadlock_otp!deadlock_otp@deadlock_otp.tmi.twitch.tv PRIVMSG #deadlock_otp :hello",
        )
        await chat._handle_privmsg(
            {"user-id": "777"},
            ":deadlock_otp!deadlock_otp@deadlock_otp.tmi.twitch.tv PRIVMSG #deadlock_otp :!link ABCD1234",
        )
        await asyncio.sleep(0)

        self.assertEqual(storage.messages, [])
        self.assertEqual(
            storage.presence_events,
            [
                ("deadlock_otp", "777", False, False),
                ("deadlock_otp", "777", False, False),
            ],
        )
        self.assertEqual(storage.claims, [("ABCD1234", "deadlock_otp", "777")])
        self.assertEqual(linked, [(101, "Alice")])
        self.assertEqual(state.messages_seen, 2)
        self.assertEqual(state.link_attempts, 1)
        self.assertEqual(state.successful_links, 1)
        self.assertTrue(state.connected)

    async def test_message_is_not_recorded_when_tracking_disabled(self) -> None:
        storage = FakeStorage()

        async def notify_linked(_telegram_user_id: int, _name: str) -> None:
            raise AssertionError("link callback should not be called")

        chat = TwitchChat(
            channel="deadlock_otp",
            bot_login="deadlock_otp",
            oauth_token="token",
            storage=storage,  # type: ignore[arg-type]
            notify_linked=notify_linked,
            tracking_enabled=lambda: False,
        )

        await chat._handle_privmsg(
            {"user-id": "778"},
            ":viewer_tv!viewer_tv@viewer_tv.tmi.twitch.tv PRIVMSG #deadlock_otp :hello",
        )

        self.assertEqual(storage.messages, [])
        self.assertEqual(
            storage.presence_events,
            [("viewer_tv", "778", False, False)],
        )

    async def test_channel_owner_message_is_counted_when_not_excluded(self) -> None:
        storage = FakeStorage()

        async def notify_linked(_telegram_user_id: int, _name: str) -> None:
            return None

        chat = TwitchChat(
            channel="deadlock_otp",
            bot_login="deadlock_otp",
            oauth_token="token",
            storage=storage,  # type: ignore[arg-type]
            notify_linked=notify_linked,
            tracking_enabled=lambda: True,
        )

        await chat._handle_privmsg(
            {"user-id": "777"},
            ":deadlock_otp!deadlock_otp@deadlock_otp.tmi.twitch.tv PRIVMSG #deadlock_otp :hello",
        )

        self.assertEqual(storage.messages, [("deadlock_otp", "777")])

    async def test_bot_can_send_sanitized_twitch_announcement_without_counting_echo(self) -> None:
        storage = FakeStorage()

        async def notify_linked(_telegram_user_id: int, _name: str) -> None:
            return None

        state = TwitchChatState()
        state.mark_connected()
        chat = TwitchChat(
            channel="deadlock_otp",
            bot_login="deadlock_otp",
            oauth_token="token",
            storage=storage,  # type: ignore[arg-type]
            notify_linked=notify_linked,
            state=state,
            tracking_enabled=lambda: True,
        )
        websocket = FakeWebSocket()
        chat._websocket = websocket  # type: ignore[assignment]
        raw_text = "Анонс\r\n\x00 " + "я" * 500

        sent = await chat.send_chat_message(raw_text)

        self.assertTrue(sent)
        self.assertEqual(len(websocket.sent), 1)
        payload = websocket.sent[0].split(" :", 1)[1]
        self.assertNotIn("\r", payload)
        self.assertNotIn("\n", payload)
        self.assertNotIn("\x00", payload)
        self.assertLessEqual(len(payload.encode("utf-8")), 400)
        self.assertEqual(payload, sanitize_chat_message(raw_text))
        self.assertIsNotNone(state.last_chat_send_at)

        await chat._handle_privmsg(
            {"user-id": "777"},
            ":deadlock_otp!deadlock_otp@deadlock_otp.tmi.twitch.tv "
            f"PRIVMSG #deadlock_otp :{payload}",
        )

        self.assertEqual(storage.messages, [])
        self.assertEqual(
            storage.presence_events,
            [("deadlock_otp", "777", False, True)],
        )

    async def test_twitch_announcement_is_not_sent_while_tracking_is_paused(self) -> None:
        storage = FakeStorage()

        async def notify_linked(_telegram_user_id: int, _name: str) -> None:
            return None

        state = TwitchChatState()
        state.mark_connected()
        chat = TwitchChat(
            channel="deadlock_otp",
            bot_login="deadlock_otp",
            oauth_token="token",
            storage=storage,  # type: ignore[arg-type]
            notify_linked=notify_linked,
            state=state,
            tracking_enabled=lambda: False,
        )
        websocket = FakeWebSocket()
        chat._websocket = websocket  # type: ignore[assignment]

        self.assertFalse(await chat.send_chat_message("Анонс"))
        self.assertEqual(websocket.sent, [])

    async def test_link_message_starts_timing_but_is_not_counted_as_comment(self) -> None:
        storage = FakeStorage()
        linked: list[tuple[int, str]] = []

        async def notify_linked(telegram_user_id: int, name: str) -> None:
            linked.append((telegram_user_id, name))

        chat = TwitchChat(
            channel="deadlock_otp",
            bot_login="deadlock_otp",
            oauth_token="token",
            storage=storage,  # type: ignore[arg-type]
            notify_linked=notify_linked,
            tracking_enabled=lambda: True,
        )

        await chat._handle_privmsg(
            {"user-id": "778"},
            ":viewer_tv!viewer_tv@viewer_tv.tmi.twitch.tv PRIVMSG #deadlock_otp :!link ABCD1234",
        )
        await asyncio.sleep(0)

        self.assertEqual(storage.messages, [])
        self.assertEqual(
            storage.presence_events,
            [("viewer_tv", "778", False, True)],
        )
        self.assertEqual(storage.claims, [("ABCD1234", "viewer_tv", "778")])
        self.assertEqual(linked, [(101, "Alice")])

    async def test_slow_telegram_notice_does_not_block_twitch_chat_reader(self) -> None:
        storage = FakeStorage()
        release_notice = asyncio.Event()

        async def notify_linked(_telegram_user_id: int, _name: str) -> None:
            await release_notice.wait()

        chat = TwitchChat(
            channel="deadlock_otp",
            bot_login="deadlock_otp",
            oauth_token="token",
            storage=storage,  # type: ignore[arg-type]
            notify_linked=notify_linked,
            tracking_enabled=lambda: True,
        )

        await asyncio.wait_for(
            chat._handle_privmsg(
                {"user-id": "778"},
                ":viewer_tv!viewer_tv@viewer_tv.tmi.twitch.tv PRIVMSG #deadlock_otp :!link ABCD1234",
            ),
            timeout=1,
        )

        self.assertEqual(storage.claims, [("ABCD1234", "viewer_tv", "778")])
        self.assertEqual(len(chat._notice_tasks), 1)
        chat.stop()
        await asyncio.sleep(0)

    async def test_ordinary_message_without_user_id_is_still_counted_by_login(self) -> None:
        storage = FakeStorage()

        async def notify_linked(_telegram_user_id: int, _name: str) -> None:
            return None

        chat = TwitchChat(
            channel="deadlock_otp",
            bot_login="deadlock_otp",
            oauth_token="token",
            storage=storage,  # type: ignore[arg-type]
            notify_linked=notify_linked,
            tracking_enabled=lambda: True,
        )

        await chat._handle_privmsg(
            {},
            ":viewer_tv!viewer_tv@viewer_tv.tmi.twitch.tv PRIVMSG #deadlock_otp :hello",
        )

        self.assertEqual(storage.messages, [("viewer_tv", None)])

    async def test_link_without_user_id_records_presence_but_is_not_claimed(self) -> None:
        storage = FakeStorage()

        async def notify_linked(_telegram_user_id: int, _name: str) -> None:
            raise AssertionError("link callback should not be called")

        chat = TwitchChat(
            channel="deadlock_otp",
            bot_login="deadlock_otp",
            oauth_token="token",
            storage=storage,  # type: ignore[arg-type]
            notify_linked=notify_linked,
            tracking_enabled=lambda: True,
        )

        await chat._handle_privmsg(
            {},
            ":viewer_tv!viewer_tv@viewer_tv.tmi.twitch.tv PRIVMSG #deadlock_otp :!link ABCD1234",
        )

        self.assertEqual(
            storage.presence_events,
            [("viewer_tv", None, False, True)],
        )
        self.assertEqual(storage.claims, [])


class LinkCodeParserTests(unittest.TestCase):
    def test_link_code_accepts_harmless_chat_formatting(self) -> None:
        self.assertEqual(parse_link_code("  !LiNk    abcd1234  "), "ABCD1234")
        self.assertEqual(parse_link_code("！ｌｉｎｋ\u200b ABCD1234"), "ABCD1234")
        self.assertEqual(
            parse_link_code("\x01ACTION !link ABCD1234\x01"),
            "ABCD1234",
        )


if __name__ == "__main__":
    unittest.main()
