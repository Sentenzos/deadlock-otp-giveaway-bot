from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import re
import time
import unicodedata

import aiohttp
import websockets

from .storage import Storage

logger = logging.getLogger(__name__)

TWITCH_WEBSOCKET_PING_INTERVAL_SECONDS = 30
TWITCH_WEBSOCKET_PING_TIMEOUT_SECONDS = 30

LinkNotice = Callable[[int, str], Awaitable[None]]
TrackingEnabled = Callable[[], bool]
LiveChanged = Callable[[bool], Awaitable[None]]
GiveawayQueryResponse = Callable[[], Awaitable[str]]

_PREFIX_RE = re.compile(r"^:(?P<login>[^! ]+)![^ ]+ ")
_NAMES_RE = re.compile(r" 353 [^ ]+ = #(?P<channel>[^ ]+) :(?P<names>.*)$")
_LINK_RE = re.compile(r"^!link\s+([A-Z0-9]{8})\s*$", re.IGNORECASE)
_GIVEAWAY_QUERY_RE = re.compile(r"^!розыгрыш\s*$", re.IGNORECASE)
_MAX_CHAT_MESSAGE_BYTES = 400
GIVEAWAY_QUERY_COOLDOWN_SECONDS = 30


def parse_link_code(text: str) -> str | None:
    cleaned = _clean_chat_command(text)
    match = _LINK_RE.fullmatch(cleaned)
    return match.group(1).upper() if match is not None else None


def is_giveaway_query(text: str) -> bool:
    return _GIVEAWAY_QUERY_RE.fullmatch(_clean_chat_command(text)) is not None


def _clean_chat_command(text: str) -> str:
    cleaned = unicodedata.normalize("NFKC", text)
    cleaned = cleaned.replace("\u200b", "").replace("\ufeff", "").strip()
    if cleaned.startswith("\x01ACTION ") and cleaned.endswith("\x01"):
        cleaned = cleaned[len("\x01ACTION ") : -1].strip()
    return cleaned


def sanitize_chat_message(text: str) -> str:
    cleaned = _normalize_chat_message(text)
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= _MAX_CHAT_MESSAGE_BYTES:
        return cleaned
    return encoded[:_MAX_CHAT_MESSAGE_BYTES].decode("utf-8", errors="ignore").rstrip()


def split_chat_message(text: str) -> list[str]:
    """Split a logical reply at field separators without losing trailing links."""
    cleaned = _normalize_chat_message(text)
    if not cleaned:
        return []
    if len(cleaned.encode("utf-8")) <= _MAX_CHAT_MESSAGE_BYTES:
        return [cleaned]

    chunks: list[str] = []
    current = ""
    for field in cleaned.split(" | "):
        candidate = f"{current} | {field}" if current else field
        if len(candidate.encode("utf-8")) <= _MAX_CHAT_MESSAGE_BYTES:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = sanitize_chat_message(field)
    if current:
        chunks.append(current)
    return chunks


def _normalize_chat_message(text: str) -> str:
    return " ".join(
        text.replace("\r", " ").replace("\n", " ").replace("\x00", " ").split()
    )


@dataclass(slots=True)
class TwitchChatState:
    connected: bool = False
    last_connected_at: int | None = None
    last_disconnected_at: int | None = None
    last_irc_at: int | None = None
    last_privmsg_at: int | None = None
    last_error: str | None = None
    last_notice: str | None = None
    stream_live: bool = False
    stream_known: bool = False
    stream_title: str | None = None
    stream_started_at: str | None = None
    last_stream_check_at: int | None = None
    last_stream_error: str | None = None
    messages_seen: int = 0
    link_attempts: int = 0
    successful_links: int = 0
    last_chat_send_at: int | None = None
    last_chat_send_error: str | None = None

    def mark_connected(self) -> None:
        now = int(time.time())
        self.connected = True
        self.last_connected_at = now
        self.last_irc_at = now
        self.last_error = None

    def mark_line_seen(self) -> None:
        self.last_irc_at = int(time.time())

    def mark_disconnected(self, error: str | None = None) -> None:
        self.connected = False
        self.last_disconnected_at = int(time.time())
        if error:
            self.last_error = error

    def mark_notice(self, notice: str) -> None:
        self.last_notice = notice

    def mark_privmsg_seen(self) -> None:
        self.messages_seen += 1
        self.last_privmsg_at = int(time.time())

    def mark_link_attempt(self) -> None:
        self.link_attempts += 1

    def mark_successful_link(self) -> None:
        self.successful_links += 1

    def mark_chat_message_sent(self) -> None:
        self.last_chat_send_at = int(time.time())
        self.last_chat_send_error = None

    def mark_chat_send_error(self, error: str) -> None:
        self.last_chat_send_error = error

    def mark_stream_live(self, title: str | None, started_at: str | None) -> None:
        self.stream_live = True
        self.stream_known = True
        self.stream_title = title
        self.stream_started_at = started_at
        self.last_stream_check_at = int(time.time())
        self.last_stream_error = None

    def mark_stream_offline(self) -> None:
        self.stream_live = False
        self.stream_known = True
        self.stream_title = None
        self.stream_started_at = None
        self.last_stream_check_at = int(time.time())
        self.last_stream_error = None

    def mark_stream_error(self, error: str) -> None:
        self.last_stream_check_at = int(time.time())
        self.last_stream_error = error


def parse_tags(line: str) -> tuple[dict[str, str], str]:
    """Return IRCv3 tags and the remaining message."""
    if not line.startswith("@"):
        return {}, line
    raw_tags, rest = line[1:].split(" ", 1)
    tags: dict[str, str] = {}
    for item in raw_tags.split(";"):
        key, _, value = item.partition("=")
        tags[key] = value
    return tags, rest


class TwitchChat:
    """Tracks viewers in a single Twitch chat using Twitch IRC over WebSocket."""

    def __init__(
        self,
        *,
        channel: str,
        bot_login: str,
        oauth_token: str,
        storage: Storage,
        notify_linked: LinkNotice,
        state: TwitchChatState | None = None,
        tracking_enabled: TrackingEnabled | None = None,
        excluded_logins: tuple[str, ...] = (),
        giveaway_query_response: GiveawayQueryResponse | None = None,
        giveaway_query_cooldown_seconds: int = GIVEAWAY_QUERY_COOLDOWN_SECONDS,
    ) -> None:
        self.channel = channel.lower()
        self.bot_login = bot_login.lower()
        self.oauth_token = oauth_token.removeprefix("oauth:")
        self.storage = storage
        self.notify_linked = notify_linked
        self.state = state or TwitchChatState()
        self.tracking_enabled = tracking_enabled or (lambda: True)
        self.excluded_logins = {
            login.strip().lower().lstrip("@") for login in excluded_logins if login.strip()
        }
        self.giveaway_query_response = giveaway_query_response
        self.giveaway_query_cooldown_seconds = giveaway_query_cooldown_seconds
        self._stop = asyncio.Event()
        self._notice_tasks: set[asyncio.Task[None]] = set()
        self._send_lock = asyncio.Lock()
        self._websocket: websockets.ClientConnection | None = None
        self._automated_messages: deque[tuple[str, float]] = deque(maxlen=20)
        self._session_ready = False
        self._last_giveaway_query_response_at: float | None = None

    def stop(self) -> None:
        self._stop.set()
        for task in tuple(self._notice_tasks):
            task.cancel()

    async def wait_for_notices(self) -> None:
        tasks = tuple(self._notice_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_irc_line(
        self, websocket: websockets.ClientConnection, line: str
    ) -> None:
        async with self._send_lock:
            if self._websocket is not websocket:
                raise ConnectionError("Twitch IRC-соединение уже сменилось")
            await websocket.send(line)

    async def send_chat_message(self, text: str, *, allow_offline: bool = False) -> bool:
        cleaned = sanitize_chat_message(text)
        websocket = self._websocket
        if not cleaned or websocket is None or not self.state.connected:
            return False
        try:
            async with self._send_lock:
                if (
                    self._websocket is not websocket
                    or not self.state.connected
                    or (not allow_offline and not self.tracking_enabled())
                ):
                    return False
                await websocket.send(f"PRIVMSG #{self.channel} :{cleaned}")
        except Exception as error:
            self.state.mark_chat_send_error(f"{type(error).__name__}: {error}")
            logger.warning("Не удалось отправить сообщение в Twitch-чат", exc_info=True)
            return False
        self._automated_messages.append((cleaned, time.monotonic() + 60))
        self.state.mark_chat_message_sent()
        logger.info("Анонс розыгрыша отправлен в Twitch-чат #%s", self.channel)
        return True

    def _consume_automated_message(self, login: str, text: str) -> bool:
        if login != self.bot_login:
            return False
        now = time.monotonic()
        while self._automated_messages and self._automated_messages[0][1] < now:
            self._automated_messages.popleft()
        for message in tuple(self._automated_messages):
            if message[0] == text:
                self._automated_messages.remove(message)
                return True
        return False

    def _schedule_link_notice(self, telegram_user_id: int, telegram_name: str) -> None:
        task = asyncio.create_task(
            self.notify_linked(telegram_user_id, telegram_name),
            name=f"telegram-link-notice-{telegram_user_id}",
        )
        self._notice_tasks.add(task)
        task.add_done_callback(self._link_notice_finished)

    def _link_notice_finished(self, task: asyncio.Task[None]) -> None:
        self._notice_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Не удалось отправить Telegram-подтверждение привязки",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def run_forever(self) -> None:
        retry_delay = 2
        while not self._stop.is_set():
            try:
                await self._connect_once()
                retry_delay = 2
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.state.mark_disconnected(f"{type(error).__name__}: {error}")
                logger.exception("Соединение с Twitch-чатом оборвалось")
            if not self._stop.is_set():
                # A session that reached JOIN/ROOMSTATE was healthy. Reconnect it
                # promptly instead of retaining an old exponential backoff from
                # failures that happened before it became ready.
                if self._session_ready:
                    retry_delay = 2
                await asyncio.sleep(retry_delay)
                if not self._session_ready:
                    retry_delay = min(retry_delay * 2, 60)

    async def _connect_once(self) -> None:
        self._session_ready = False
        logger.info("Подключаюсь к Twitch-чату #%s", self.channel)
        async with websockets.connect(
            "wss://irc-ws.chat.twitch.tv:443",
            ping_interval=TWITCH_WEBSOCKET_PING_INTERVAL_SECONDS,
            ping_timeout=TWITCH_WEBSOCKET_PING_TIMEOUT_SECONDS,
            close_timeout=5,
        ) as websocket:
            self._websocket = websocket
            try:
                await self._send_irc_line(websocket, f"PASS oauth:{self.oauth_token}")
                await self._send_irc_line(websocket, f"NICK {self.bot_login}")
                await self._send_irc_line(
                    websocket,
                    "CAP REQ :twitch.tv/membership twitch.tv/tags twitch.tv/commands",
                )
                await self._send_irc_line(websocket, f"JOIN #{self.channel}")
                logger.info("Запросил вход в Twitch-чат #%s", self.channel)

                while not self._stop.is_set():
                    # A quiet IRC channel is valid. ``websockets`` independently
                    # sends WebSocket Ping frames and closes a genuinely dead
                    # connection when no Pong arrives within ping_timeout.
                    payload = await websocket.recv()
                    for line in str(payload).split("\r\n"):
                        if line:
                            await self._handle_line(websocket, line)
            finally:
                if self._websocket is websocket:
                    self._websocket = None
                self.state.mark_disconnected()
                await self.storage.reset_chat_session(preserve_elapsed=True)

    async def _handle_line(self, websocket: websockets.ClientConnection, line: str) -> None:
        self.state.mark_line_seen()
        if line.startswith("PING "):
            await self._send_irc_line(websocket, f"PONG {line[5:]}")
            logger.debug("Twitch PING/PONG ok")
            return
        if " RECONNECT" in line:
            logger.warning("Twitch запросил RECONNECT")
            await websocket.close()
            return

        tags, message = parse_tags(line)
        if " 001 " in message:
            logger.info("Twitch принял логин %s", self.bot_login)
            return
        if " CAP " in message:
            logger.info("Twitch capability response: %s", message)
            return
        if " NOTICE " in message and " :" in message:
            notice = message.split(" :", 1)[1]
            self.state.mark_notice(notice)
            logger.warning("Twitch NOTICE: %s", notice)
            if "Login authentication failed" in notice or "Improperly formatted auth" in notice:
                raise PermissionError(notice)
            return
        if " ROOMSTATE #" in message and self._is_our_channel(message):
            self._session_ready = True
            self.state.mark_connected()
            logger.info("Twitch-чат #%s готов: ROOMSTATE получен", self.channel)
            return
        if " JOIN #" in message:
            login = self._login_from(message)
            if login is not None and self._is_our_channel(message):
                if login == self.bot_login and not self.state.connected:
                    self._session_ready = True
                    self.state.mark_connected()
                await self.storage.mark_joined(
                    login,
                    tags.get("user-id"),
                    count_time=self.tracking_enabled() and login not in self.excluded_logins,
                )
            return
        if " PART #" in message:
            login = self._login_from(message)
            if login is not None and self._is_our_channel(message):
                await self.storage.mark_parted(login)
            return
        if " 353 " in message:
            names = _NAMES_RE.search(message)
            if names is not None and names["channel"].lower() == self.channel:
                self._session_ready = True
                self.state.mark_connected()
                logger.info("Twitch-чат #%s готов: получен список участников", self.channel)
                for login in names["names"].split():
                    clean_login = login.lstrip("@+%").lower()
                    await self.storage.mark_joined(
                        clean_login,
                        count_time=(
                            self.tracking_enabled() and clean_login not in self.excluded_logins
                        ),
                    )
            return
        if " PRIVMSG #" in message:
            await self._handle_privmsg(tags, message)

    async def _handle_privmsg(self, tags: dict[str, str], message: str) -> None:
        if not self._is_our_channel(message):
            return
        login = self._login_from(message)
        twitch_user_id = tags.get("user-id") or None
        if login is None:
            logger.warning(
                "Пропускаю Twitch PRIVMSG без login: %s", message
            )
            return
        if not self.state.connected:
            self._session_ready = True
            self.state.mark_connected()
        self.state.mark_privmsg_seen()
        text = message.split(" :", 1)[1] if " :" in message else ""
        count_time = self.tracking_enabled() and login not in self.excluded_logins
        if self._consume_automated_message(login, text):
            await self.storage.record_message(
                login,
                twitch_user_id,
                count_message=False,
                count_time=count_time,
            )
            return
        link_code = parse_link_code(text)
        if link_code is not None:
            await self.storage.record_message(
                login,
                twitch_user_id,
                count_message=False,
                count_time=count_time,
            )
            self.state.mark_link_attempt()
            if twitch_user_id is None:
                logger.warning(
                    "Не могу привязать Twitch %s: PRIVMSG не содержит user-id",
                    login,
                )
                return
            status, telegram_id, telegram_name = await self.storage.claim_link_code(
                link_code, login, twitch_user_id
            )
            if status == "linked" and telegram_id is not None:
                self.state.mark_successful_link()
                self._schedule_link_notice(telegram_id, telegram_name or "участник")
            elif status == "already_linked":
                logger.warning("Twitch-аккаунт %s уже привязан к другому Telegram", login)
            else:
                logger.warning("Не удалось привязать Twitch %s: %s", login, status)
            return
        if is_giveaway_query(text):
            await self.storage.record_message(
                login,
                twitch_user_id,
                count_message=False,
                count_time=count_time,
            )
            await self._answer_giveaway_query()
            return
        if not count_time:
            await self.storage.record_message(
                login,
                twitch_user_id,
                count_message=False,
                count_time=False,
            )
            if login in self.excluded_logins:
                logger.debug("Не учитываю сообщение исключённого Twitch-логина %s", login)
            else:
                logger.debug("Не учитываю сообщение %s: стрим сейчас не live", login)
            return
        await self.storage.record_message(login, twitch_user_id)

    async def _answer_giveaway_query(self) -> None:
        if self.giveaway_query_response is None:
            return
        now = time.monotonic()
        last_response_at = self._last_giveaway_query_response_at
        if (
            last_response_at is not None
            and now - last_response_at < self.giveaway_query_cooldown_seconds
        ):
            return
        self._last_giveaway_query_response_at = now
        try:
            response = await self.giveaway_query_response()
            chunks = split_chat_message(response)
        except Exception:
            self._last_giveaway_query_response_at = last_response_at
            logger.exception("Не удалось подготовить ответ на !розыгрыш")
            return
        sent_any = False
        for chunk in chunks:
            if not await self.send_chat_message(chunk, allow_offline=True):
                if not sent_any:
                    self._last_giveaway_query_response_at = last_response_at
                return
            sent_any = True
        if not sent_any:
            self._last_giveaway_query_response_at = last_response_at

    def _is_our_channel(self, line: str) -> bool:
        line = line.lower()
        marker = f" #{self.channel}"
        position = line.find(marker)
        if position < 0:
            return False
        next_position = position + len(marker)
        return next_position == len(line) or line[next_position].isspace()

    @staticmethod
    def _login_from(message: str) -> str | None:
        match = _PREFIX_RE.match(message)
        return match["login"].lower() if match else None


class TwitchLiveMonitor:
    def __init__(
        self,
        *,
        channel: str,
        oauth_token: str,
        state: TwitchChatState,
        on_live_changed: LiveChanged,
        poll_interval_seconds: int = 10,
    ) -> None:
        self.channel = channel.lower().lstrip("#")
        self.oauth_token = oauth_token.removeprefix("oauth:")
        self.state = state
        self.on_live_changed = on_live_changed
        self.poll_interval_seconds = max(5, poll_interval_seconds)
        self._stop = asyncio.Event()
        self._check_lock = asyncio.Lock()
        self._client_id: str | None = None
        self._token_user_id: str | None = None
        self._token_scopes: frozenset[str] = frozenset()
        self._broadcaster_id: str | None = None

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("Не удалось проверить live-статус Twitch")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval_seconds)
            except TimeoutError:
                pass

    async def refresh(self) -> None:
        """Refresh live state now, serializing manual and scheduled checks."""
        async with self._check_lock:
            await self._refresh_unlocked()

    async def refresh_if_stale(self, max_age_seconds: int = 3) -> None:
        """Refresh unless another recent check already supplied a current answer."""
        async with self._check_lock:
            last_check = self.state.last_stream_check_at
            if last_check is not None and int(time.time()) - last_check <= max_age_seconds:
                return
            await self._refresh_unlocked()

    async def _refresh_unlocked(self) -> None:
        try:
            await self._check_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.state.mark_stream_error(f"{type(error).__name__}: {error}")
            raise

    async def _check_once(self) -> None:
        async with aiohttp.ClientSession() as session:
            client_id = await self._client_id_for_token(session)
            stream = await self._fetch_stream(session, client_id)
        was_live = self.state.stream_live
        if stream is None:
            self.state.mark_stream_offline()
            if was_live:
                logger.info("Twitch-стрим #%s завершён: останавливаю учёт", self.channel)
                await self.on_live_changed(False)
            else:
                logger.debug("Twitch-стрим #%s сейчас offline", self.channel)
            return

        self.state.mark_stream_live(stream.get("title"), stream.get("started_at"))
        if not was_live:
            logger.info("Twitch-стрим #%s live: включаю учёт", self.channel)
            await self.on_live_changed(True)
        else:
            logger.debug("Twitch-стрим #%s всё ещё live", self.channel)

    async def _client_id_for_token(self, session: aiohttp.ClientSession) -> str:
        if self._client_id is not None and self._token_user_id is not None:
            return self._client_id
        async with session.get(
            "https://id.twitch.tv/oauth2/validate",
            headers={"Authorization": f"OAuth {self.oauth_token}"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            payload = await response.json(content_type=None)
            if response.status != 200:
                raise RuntimeError(f"Twitch validate вернул HTTP {response.status}: {payload}")
        client_id = str(payload.get("client_id") or "")
        user_id = str(payload.get("user_id") or "")
        if not client_id or not user_id:
            raise RuntimeError(
                "Twitch validate не вернул client_id/user_id для User OAuth-токена"
            )
        self._client_id = client_id
        self._token_user_id = user_id
        self._token_scopes = frozenset(str(scope) for scope in payload.get("scopes") or [])
        return client_id

    async def fetch_chatters(self) -> list[str]:
        """Return Twitch logins currently connected to the channel's chat."""
        async with aiohttp.ClientSession() as session:
            client_id = await self._client_id_for_token(session)
            if "moderator:read:chatters" not in self._token_scopes:
                raise RuntimeError(
                    "OAuth-токен Twitch не содержит право moderator:read:chatters. "
                    "Создайте User Access Token с правами chat:read и "
                    "moderator:read:chatters."
                )
            moderator_id = self._token_user_id
            if moderator_id is None:
                raise RuntimeError("Twitch не вернул ID владельца OAuth-токена")
            broadcaster_id = await self._broadcaster_id_for_channel(session, client_id)
            return await self._fetch_chatters(
                session, client_id, broadcaster_id, moderator_id
            )

    async def require_chat_edit_scope(self) -> None:
        async with aiohttp.ClientSession() as session:
            await self._client_id_for_token(session)
        if "chat:edit" not in self._token_scopes:
            raise RuntimeError(
                "OAuth-токен Twitch не содержит право chat:edit, необходимое "
                "для отправки анонсов в Twitch-чат. Создайте новый User Access "
                "Token с правами chat:read и chat:edit."
            )

    async def _broadcaster_id_for_channel(
        self, session: aiohttp.ClientSession, client_id: str
    ) -> str:
        if self._broadcaster_id is not None:
            return self._broadcaster_id
        async with session.get(
            "https://api.twitch.tv/helix/users",
            params={"login": self.channel},
            headers={
                "Authorization": f"Bearer {self.oauth_token}",
                "Client-Id": client_id,
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            payload = await response.json(content_type=None)
            if response.status != 200:
                raise RuntimeError(f"Twitch users вернул HTTP {response.status}: {payload}")
        users = payload.get("data") or []
        if not users or not users[0].get("id"):
            raise RuntimeError(f"Twitch-канал {self.channel} не найден")
        self._broadcaster_id = str(users[0]["id"])
        return self._broadcaster_id

    async def _fetch_chatters(
        self,
        session: aiohttp.ClientSession,
        client_id: str,
        broadcaster_id: str,
        moderator_id: str,
    ) -> list[str]:
        chatters: dict[str, str] = {}
        cursor: str | None = None
        while True:
            params = {
                "broadcaster_id": broadcaster_id,
                "moderator_id": moderator_id,
                "first": "1000",
            }
            if cursor:
                params["after"] = cursor
            async with session.get(
                "https://api.twitch.tv/helix/chat/chatters",
                params=params,
                headers={
                    "Authorization": f"Bearer {self.oauth_token}",
                    "Client-Id": client_id,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status == 401:
                    raise RuntimeError(
                        "Twitch отклонил OAuth-токен. Проверьте право "
                        "moderator:read:chatters и срок действия токена."
                    )
                if response.status == 403:
                    raise RuntimeError(
                        "Twitch-аккаунт OAuth-токена не является владельцем "
                        "или модератором этого канала."
                    )
                if response.status != 200:
                    raise RuntimeError(
                        f"Twitch chatters вернул HTTP {response.status}: {payload}"
                    )
            for chatter in payload.get("data") or []:
                login = str(chatter.get("user_login") or "").strip().lower()
                if login:
                    chatters[login] = login
            cursor = str((payload.get("pagination") or {}).get("cursor") or "") or None
            if cursor is None:
                break
        return sorted(chatters.values(), key=str.casefold)

    async def _fetch_stream(
        self, session: aiohttp.ClientSession, client_id: str
    ) -> dict[str, str] | None:
        async with session.get(
            "https://api.twitch.tv/helix/streams",
            params={"user_login": self.channel},
            headers={
                "Authorization": f"Bearer {self.oauth_token}",
                "Client-Id": client_id,
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            payload = await response.json(content_type=None)
            if response.status != 200:
                raise RuntimeError(f"Twitch streams вернул HTTP {response.status}: {payload}")
        streams = payload.get("data") or []
        if not streams:
            return None
        stream = streams[0]
        return stream if str(stream.get("type", "")).lower() == "live" else None
