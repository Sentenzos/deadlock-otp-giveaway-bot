from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
import logging
import time

from .storage import Giveaway, Storage
from .twitch_chat import TwitchChatState

logger = logging.getLogger(__name__)

SendChatMessage = Callable[[str], Awaitable[bool]]
MOSCOW_TIMEZONE = timezone(timedelta(hours=3), name="МСК")


def giveaway_twitch_chat_announcement(
    giveaway: Giveaway, bot_username: str, announced_at: int | None = None
) -> str:
    timestamp = int(time.time()) if announced_at is None else announced_at
    clock = datetime.fromtimestamp(timestamp, MOSCOW_TIMEZONE).strftime("%H:%M МСК")
    link = f"https://t.me/{bot_username.lstrip('@')}?start=link"
    parts = [
        "🎉 Идёт розыгрыш!",
        f"Участие: {link}",
        f"Название: {giveaway.title}",
    ]
    if giveaway.prize:
        parts.append(f"Награда: {giveaway.prize}")
    parts.extend(
        [
            (
                f"Условия: {giveaway.min_seconds // 60} мин в чате и "
                f"{giveaway.min_messages} сообщений"
            ),
            f"Анонс: {clock}",
        ]
    )
    return " | ".join(parts)


def giveaway_twitch_chat_query_response(
    giveaway: Giveaway | None,
    bot_username: str,
    telegram_channel_url: str,
) -> str:
    """Build the approved public response to ``!розыгрыш``."""
    channel_link = telegram_channel_url.rstrip("/")
    if giveaway is None:
        return (
            "Сейчас активного розыгрыша нет. Следите за анонсами: "
            f"{channel_link}"
        )

    registration_link = f"https://t.me/{bot_username.lstrip('@')}?start=link"
    parts = [f"🎁 Розыгрыш «{giveaway.title}»"]
    if giveaway.prize:
        parts.append(f"Приз: {giveaway.prize}")
    conditions = (
        f"Условия: {giveaway.min_seconds // 60} мин просмотра трансляции и "
        f"{giveaway.min_messages} сообщений"
    )
    if giveaway.message_interval_seconds > 0:
        conditions += f" (интервал от {giveaway.message_interval_seconds} сек)"
    parts.extend([conditions, f"Победителей: {giveaway.winner_count}"])
    if giveaway.end_at is not None:
        end_text = datetime.fromtimestamp(giveaway.end_at, MOSCOW_TIMEZONE).strftime(
            "%d.%m.%Y %H:%M МСК"
        )
        parts.append(f"Завершение: {end_text}")
    parts.extend(
        [
            f"Telegram: {channel_link}",
            f"Регистрация: {registration_link}",
        ]
    )
    return " | ".join(parts)


class TwitchGiveawayAnnouncer:
    def __init__(
        self,
        *,
        storage: Storage,
        twitch_state: TwitchChatState,
        send_chat_message: SendChatMessage,
        bot_username: str,
        check_interval_seconds: float = 5,
        failure_retry_seconds: int = 60,
    ) -> None:
        self.storage = storage
        self.twitch_state = twitch_state
        self.send_chat_message = send_chat_message
        self.bot_username = bot_username
        self.check_interval_seconds = max(0.1, check_interval_seconds)
        self.failure_retry_seconds = max(1, failure_retry_seconds)
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_failed_attempt: tuple[int, int] | None = None
        self._last_successful_send: tuple[int, int] | None = None
        self._pending_persistence: tuple[int, int, int] | None = None

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def configure(
        self, enabled: bool, interval_minutes: int | None = None
    ) -> Giveaway | None:
        async with self._lock:
            giveaway = await self.storage.configure_twitch_announcements(
                enabled=enabled,
                interval_minutes=interval_minutes,
            )
            self._last_failed_attempt = None
            if giveaway is None or not enabled:
                self._last_successful_send = None
                self._pending_persistence = None
            elif (
                self._last_successful_send is not None
                and self._last_successful_send[0] != giveaway.id
            ):
                self._last_successful_send = None
                self._pending_persistence = None
        self.wake()
        return giveaway

    async def announce_if_due(self, now: int | None = None) -> bool:
        async with self._lock:
            giveaway = await self.storage.active_giveaway()
            if giveaway is None or not giveaway.twitch_announce_enabled:
                return False
            if (
                not self.twitch_state.stream_live
                or self.twitch_state.last_stream_error is not None
                or not self.twitch_state.connected
            ):
                return False

            current_time = int(time.time()) if now is None else now
            if (
                self._pending_persistence is not None
                and self._pending_persistence[0] == giveaway.id
                and current_time >= self._pending_persistence[2]
            ):
                pending_giveaway_id, pending_sent_at, _ = self._pending_persistence
                try:
                    await self.storage.mark_twitch_announcement_sent(
                        pending_giveaway_id, pending_sent_at
                    )
                except Exception:
                    self._pending_persistence = (
                        pending_giveaway_id,
                        pending_sent_at,
                        current_time + self.failure_retry_seconds,
                    )
                    logger.exception(
                        "Не удалось сохранить время Twitch-анонса; повторю запись позже"
                    )
                else:
                    self._pending_persistence = None

            last_announce_at = giveaway.twitch_last_announce_at
            if (
                self._last_successful_send is not None
                and self._last_successful_send[0] == giveaway.id
            ):
                memory_sent_at = self._last_successful_send[1]
                last_announce_at = max(last_announce_at or 0, memory_sent_at)
            if (
                last_announce_at is not None
                and current_time - last_announce_at
                < giveaway.twitch_announce_interval_seconds
            ):
                return False
            if (
                self._last_failed_attempt is not None
                and self._last_failed_attempt[0] == giveaway.id
                and current_time - self._last_failed_attempt[1]
                < self.failure_retry_seconds
            ):
                return False

            message = giveaway_twitch_chat_announcement(
                giveaway, self.bot_username, current_time
            )
            if not await self.send_chat_message(message):
                self._last_failed_attempt = (giveaway.id, current_time)
                return False

            self._last_successful_send = (giveaway.id, current_time)
            try:
                await self.storage.mark_twitch_announcement_sent(
                    giveaway.id, current_time
                )
            except Exception:
                self._pending_persistence = (
                    giveaway.id,
                    current_time,
                    current_time + self.failure_retry_seconds,
                )
                logger.exception(
                    "Twitch-анонс отправлен, но его время пока не сохранено"
                )
            else:
                self._pending_persistence = None
            self._last_failed_attempt = None
            return True

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.announce_if_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка периодического анонса розыгрыша в Twitch-чате")
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.check_interval_seconds
                )
            except TimeoutError:
                pass
            finally:
                self._wake.clear()
