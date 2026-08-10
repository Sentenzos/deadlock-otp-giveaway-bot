from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import secrets
import time

import aiosqlite


@dataclass(frozen=True, slots=True)
class Giveaway:
    id: int
    state: str
    title: str
    prize: str
    winner_count: int
    min_participants: int
    min_seconds: int
    min_messages: int
    message_interval_seconds: int
    started_at: int
    finished_at: int | None
    eligible_count_at_finish: int | None
    end_at: int | None = None
    twitch_announce_enabled: bool = False
    twitch_announce_interval_seconds: int = 15 * 60
    twitch_last_announce_at: int | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    telegram_user_id: int
    telegram_name: str
    twitch_login: str
    seconds: int
    messages: int
    telegram_username: str | None = None


@dataclass(frozen=True, slots=True)
class Participant:
    twitch_login: str
    seconds: int
    messages: int
    telegram_user_id: int | None
    telegram_name: str | None
    telegram_username: str | None = None


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.db: aiosqlite.Connection | None = None
        self._link_code_lock = asyncio.Lock()
        self._tracking_session_lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        registration_table_existed = (
            await (
                await self.db.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE type = 'table' AND name = 'giveaway_registrations'"""
                )
            ).fetchone()
            is not None
        )
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS link_codes (
                code TEXT PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL,
                telegram_name TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                giveaway_id INTEGER NOT NULL REFERENCES giveaways(id),
                telegram_username TEXT
            );

            CREATE TABLE IF NOT EXISTS twitch_links (
                twitch_login TEXT PRIMARY KEY,
                twitch_user_id TEXT NOT NULL UNIQUE,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                telegram_name TEXT NOT NULL,
                linked_at INTEGER NOT NULL,
                telegram_username TEXT
            );

            CREATE TABLE IF NOT EXISTS chat_presence (
                twitch_login TEXT PRIMARY KEY,
                twitch_user_id TEXT,
                joined_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                state TEXT NOT NULL CHECK(state IN ('active', 'finished')),
                title TEXT NOT NULL DEFAULT 'Розыгрыш',
                title_key TEXT NOT NULL DEFAULT 'розыгрыш',
                prize TEXT NOT NULL DEFAULT '',
                winner_count INTEGER NOT NULL DEFAULT 1,
                min_participants INTEGER NOT NULL DEFAULT 1,
                min_seconds INTEGER NOT NULL,
                min_messages INTEGER NOT NULL,
                message_interval_seconds INTEGER NOT NULL DEFAULT 0,
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                eligible_count_at_finish INTEGER,
                end_at INTEGER,
                twitch_announce_enabled INTEGER NOT NULL DEFAULT 0,
                twitch_announce_interval_seconds INTEGER NOT NULL DEFAULT 900,
                twitch_last_announce_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS giveaway_activity (
                giveaway_id INTEGER NOT NULL REFERENCES giveaways(id),
                twitch_login TEXT NOT NULL,
                twitch_user_id TEXT,
                seconds INTEGER NOT NULL DEFAULT 0,
                messages INTEGER NOT NULL DEFAULT 0,
                last_counted_message_at INTEGER,
                presence_started_at INTEGER,
                PRIMARY KEY (giveaway_id, twitch_login)
            );

            CREATE TABLE IF NOT EXISTS giveaway_registrations (
                giveaway_id INTEGER NOT NULL REFERENCES giveaways(id),
                twitch_login TEXT NOT NULL,
                twitch_user_id TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                telegram_name TEXT NOT NULL,
                registered_at INTEGER NOT NULL,
                telegram_username TEXT,
                PRIMARY KEY (giveaway_id, twitch_login),
                UNIQUE (giveaway_id, telegram_user_id)
            );

            CREATE TABLE IF NOT EXISTS giveaway_winners (
                giveaway_id INTEGER NOT NULL REFERENCES giveaways(id),
                telegram_user_id INTEGER NOT NULL,
                drawn_at INTEGER NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (giveaway_id, telegram_user_id)
            );

            CREATE TABLE IF NOT EXISTS runtime_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        await self._migrate()
        if not registration_table_existed:
            await self._backfill_finished_giveaway_registrations()
        await self.db.commit()

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()

    @property
    def _db(self) -> aiosqlite.Connection:
        if self.db is None:
            raise RuntimeError("База данных не подключена.")
        return self.db

    @staticmethod
    def now() -> int:
        return int(time.time())

    async def runtime_state(self, key: str) -> str | None:
        row = await (
            await self._db.execute(
                "SELECT value FROM runtime_state WHERE key = ?", (key,)
            )
        ).fetchone()
        return str(row["value"]) if row is not None else None

    async def set_runtime_state(self, key: str, value: str) -> None:
        await self._db.execute(
            """INSERT INTO runtime_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        await self._db.commit()

    @staticmethod
    def _excluded_logins(excluded_twitch_logins: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    login.strip().lower().lstrip("@")
                    for login in excluded_twitch_logins
                    if login.strip()
                }
            )
        )

    @classmethod
    def _excluded_sql(
        cls,
        excluded_twitch_logins: tuple[str, ...],
        table_alias: str = "activity",
    ) -> tuple[str, tuple[str, ...]]:
        excluded = cls._excluded_logins(excluded_twitch_logins)
        if not excluded:
            return "", ()
        placeholders = ", ".join("?" for _ in excluded)
        return f" AND {table_alias}.twitch_login NOT IN ({placeholders})", excluded

    @staticmethod
    def _excluded_telegram_usernames(
        excluded_telegram_usernames: tuple[str, ...],
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    username.strip().casefold().lstrip("@")
                    for username in excluded_telegram_usernames
                    if username.strip()
                }
            )
        )

    @classmethod
    def _registration_exclusions_sql(
        cls,
        excluded_twitch_logins: tuple[str, ...],
        excluded_telegram_usernames: tuple[str, ...],
        table_alias: str = "registration",
    ) -> tuple[str, tuple[str, ...]]:
        sql, twitch_params = cls._excluded_sql(excluded_twitch_logins, table_alias)
        telegram_usernames = cls._excluded_telegram_usernames(
            excluded_telegram_usernames
        )
        if not telegram_usernames:
            return sql, twitch_params
        placeholders = ", ".join("?" for _ in telegram_usernames)
        sql += (
            f" AND ({table_alias}.telegram_username IS NULL OR "
            f"LOWER(LTRIM({table_alias}.telegram_username, '@')) NOT IN ({placeholders}))"
        )
        return sql, (*twitch_params, *telegram_usernames)

    async def _migrate(self) -> None:
        link_code_columns = await (
            await self._db.execute("PRAGMA table_info(link_codes)")
        ).fetchall()
        link_code_column_names = {str(row["name"]) for row in link_code_columns}
        if "giveaway_id" not in link_code_column_names:
            await self._db.execute(
                "ALTER TABLE link_codes ADD COLUMN giveaway_id INTEGER REFERENCES giveaways(id)"
            )
        if "telegram_username" not in link_code_column_names:
            await self._db.execute(
                "ALTER TABLE link_codes ADD COLUMN telegram_username TEXT"
            )

        twitch_link_columns = await (
            await self._db.execute("PRAGMA table_info(twitch_links)")
        ).fetchall()
        twitch_link_column_names = {str(row["name"]) for row in twitch_link_columns}
        if "telegram_username" not in twitch_link_column_names:
            await self._db.execute(
                "ALTER TABLE twitch_links ADD COLUMN telegram_username TEXT"
            )

        giveaway_columns = await (
            await self._db.execute("PRAGMA table_info(giveaways)")
        ).fetchall()
        column_names = {str(row["name"]) for row in giveaway_columns}
        if "title" not in column_names:
            await self._db.execute(
                "ALTER TABLE giveaways ADD COLUMN title TEXT NOT NULL DEFAULT 'Розыгрыш'"
            )
        if "title_key" not in column_names:
            await self._db.execute(
                "ALTER TABLE giveaways ADD COLUMN title_key TEXT NOT NULL DEFAULT 'розыгрыш'"
            )
        if "prize" not in column_names:
            await self._db.execute(
                "ALTER TABLE giveaways ADD COLUMN prize TEXT NOT NULL DEFAULT ''"
            )
        if "winner_count" not in column_names:
            await self._db.execute(
                "ALTER TABLE giveaways ADD COLUMN winner_count INTEGER NOT NULL DEFAULT 1"
            )
        if "min_participants" not in column_names:
            await self._db.execute(
                "ALTER TABLE giveaways ADD COLUMN min_participants INTEGER NOT NULL DEFAULT 1"
            )
        if "message_interval_seconds" not in column_names:
            await self._db.execute(
                "ALTER TABLE giveaways ADD COLUMN message_interval_seconds INTEGER NOT NULL DEFAULT 0"
            )
        if "eligible_count_at_finish" not in column_names:
            await self._db.execute(
                "ALTER TABLE giveaways ADD COLUMN eligible_count_at_finish INTEGER"
            )
        if "end_at" not in column_names:
            await self._db.execute("ALTER TABLE giveaways ADD COLUMN end_at INTEGER")
        if "twitch_announce_enabled" not in column_names:
            await self._db.execute(
                "ALTER TABLE giveaways ADD COLUMN twitch_announce_enabled INTEGER NOT NULL DEFAULT 0"
            )
        if "twitch_announce_interval_seconds" not in column_names:
            await self._db.execute(
                "ALTER TABLE giveaways ADD COLUMN twitch_announce_interval_seconds INTEGER NOT NULL DEFAULT 900"
            )
        if "twitch_last_announce_at" not in column_names:
            await self._db.execute(
                "ALTER TABLE giveaways ADD COLUMN twitch_last_announce_at INTEGER"
            )
        rows = await (
            await self._db.execute("SELECT id, title, title_key FROM giveaways")
        ).fetchall()
        for row in rows:
            title_key = str(row["title"]).casefold()
            if str(row["title_key"]) != title_key:
                await self._db.execute(
                    "UPDATE giveaways SET title_key = ? WHERE id = ?",
                    (title_key, int(row["id"])),
                )
        activity_columns = await (
            await self._db.execute("PRAGMA table_info(giveaway_activity)")
        ).fetchall()
        activity_column_names = {str(row["name"]) for row in activity_columns}
        if "last_counted_message_at" not in activity_column_names:
            await self._db.execute(
                "ALTER TABLE giveaway_activity ADD COLUMN last_counted_message_at INTEGER"
            )
        registration_columns = await (
            await self._db.execute("PRAGMA table_info(giveaway_registrations)")
        ).fetchall()
        registration_column_names = {str(row["name"]) for row in registration_columns}
        if "telegram_username" not in registration_column_names:
            await self._db.execute(
                "ALTER TABLE giveaway_registrations ADD COLUMN telegram_username TEXT"
            )

    async def _backfill_finished_giveaway_registrations(self) -> None:
        """Keep old completed results readable after adding per-giveaway registration."""
        await self._db.execute(
            """INSERT OR IGNORE INTO giveaway_registrations
               (giveaway_id, twitch_login, twitch_user_id, telegram_user_id, telegram_name,
                registered_at, telegram_username)
               SELECT activity.giveaway_id,
                      activity.twitch_login,
                      link.twitch_user_id,
                      link.telegram_user_id,
                      link.telegram_name,
                      COALESCE(giveaway.finished_at, giveaway.started_at),
                      link.telegram_username
               FROM giveaway_activity AS activity
               JOIN giveaways AS giveaway ON giveaway.id = activity.giveaway_id
               JOIN twitch_links AS link ON link.twitch_login = activity.twitch_login
               WHERE giveaway.state = 'finished'"""
        )

    async def create_link_code(
        self,
        telegram_user_id: int,
        telegram_name: str,
        giveaway_id: int,
        telegram_username: str | None = None,
    ) -> str:
        async with self._link_code_lock:
            return await self._create_link_code_unlocked(
                telegram_user_id,
                telegram_name,
                giveaway_id,
                telegram_username,
            )

    async def _create_link_code_unlocked(
        self,
        telegram_user_id: int,
        telegram_name: str,
        giveaway_id: int,
        telegram_username: str | None = None,
    ) -> str:
        now = self.now()
        await self._db.execute("DELETE FROM link_codes WHERE expires_at < ?", (now,))
        existing = await (
            await self._db.execute(
                """SELECT code FROM link_codes
                   WHERE telegram_user_id = ? AND giveaway_id = ? AND expires_at >= ?
                   ORDER BY expires_at DESC LIMIT 1""",
                (telegram_user_id, giveaway_id, now),
            )
        ).fetchone()
        if existing is not None:
            code = str(existing["code"])
            await self._db.execute(
                """UPDATE link_codes
                   SET telegram_name = ?, telegram_username = ?
                   WHERE code = ?""",
                (telegram_name, telegram_username, code),
            )
            await self._db.commit()
            return code
        await self._db.execute(
            "DELETE FROM link_codes WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        # Eight characters are easy to type in chat and provide 41 bits of entropy.
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        while True:
            code = "".join(secrets.choice(alphabet) for _ in range(8))
            try:
                await self._db.execute(
                    """INSERT INTO link_codes
                       (code, telegram_user_id, telegram_name, expires_at, giveaway_id,
                        telegram_username)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        code,
                        telegram_user_id,
                        telegram_name,
                        now + 600,
                        giveaway_id,
                        telegram_username,
                    ),
                )
                await self._db.commit()
                return code
            except aiosqlite.IntegrityError:
                continue

    async def claim_link_code(
        self, code: str, twitch_login: str, twitch_user_id: str
    ) -> tuple[str, int | None, str | None]:
        async with self._link_code_lock:
            return await self._claim_link_code_unlocked(
                code, twitch_login, twitch_user_id
            )

    async def _claim_link_code_unlocked(
        self, code: str, twitch_login: str, twitch_user_id: str
    ) -> tuple[str, int | None, str | None]:
        """Claim a code from Twitch chat.

        Returns `(status, telegram_user_id, telegram_name)`, where status is
        `linked`, `missing`, `expired`, `giveaway_closed`, or `already_linked`.
        """
        now = self.now()
        row = await (
            await self._db.execute(
                """SELECT telegram_user_id, telegram_name, expires_at, giveaway_id,
                          telegram_username
                   FROM link_codes WHERE code = ?""",
                (code.upper(),),
            )
        ).fetchone()
        if row is None:
            return "missing", None, None
        if row["expires_at"] < now:
            await self._db.execute("DELETE FROM link_codes WHERE code = ?", (code.upper(),))
            await self._db.commit()
            return "expired", None, None

        giveaway = await self.active_giveaway()
        if giveaway is None or row["giveaway_id"] is None or int(row["giveaway_id"]) != giveaway.id:
            await self._db.execute("DELETE FROM link_codes WHERE code = ?", (code.upper(),))
            await self._db.commit()
            return "giveaway_closed", None, None

        existing = await (
            await self._db.execute(
                """SELECT telegram_user_id FROM twitch_links
                   WHERE twitch_user_id = ? OR twitch_login = ?""",
                (twitch_user_id, twitch_login),
            )
        ).fetchone()
        telegram_user_id = int(row["telegram_user_id"])
        telegram_name = str(row["telegram_name"])
        telegram_username = (
            str(row["telegram_username"]) if row["telegram_username"] is not None else None
        )
        if existing is not None and int(existing["telegram_user_id"]) != telegram_user_id:
            return "already_linked", None, None

        await self._db.execute(
            "DELETE FROM twitch_links WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        await self._db.execute(
            """INSERT INTO twitch_links
               (twitch_login, twitch_user_id, telegram_user_id, telegram_name, linked_at,
                telegram_username)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                twitch_login,
                twitch_user_id,
                telegram_user_id,
                telegram_name,
                now,
                telegram_username,
            ),
        )
        await self._db.execute(
            """DELETE FROM giveaway_registrations
               WHERE giveaway_id = ? AND telegram_user_id = ?""",
            (giveaway.id, telegram_user_id),
        )
        await self._db.execute(
            """INSERT INTO giveaway_registrations
               (giveaway_id, twitch_login, twitch_user_id, telegram_user_id, telegram_name,
                registered_at, telegram_username)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(giveaway_id, twitch_login) DO UPDATE SET
                   twitch_user_id = excluded.twitch_user_id,
                   telegram_user_id = excluded.telegram_user_id,
                   telegram_name = excluded.telegram_name,
                   registered_at = excluded.registered_at,
                   telegram_username = excluded.telegram_username""",
            (
                giveaway.id,
                twitch_login,
                twitch_user_id,
                telegram_user_id,
                telegram_name,
                now,
                telegram_username,
            ),
        )
        await self._db.execute("DELETE FROM link_codes WHERE code = ?", (code.upper(),))
        await self._db.commit()
        return "linked", telegram_user_id, telegram_name

    async def mark_joined(
        self, twitch_login: str, twitch_user_id: str | None = None, count_time: bool = True
    ) -> None:
        now = self.now()
        await self._db.execute(
            """INSERT INTO chat_presence(twitch_login, twitch_user_id, joined_at)
               VALUES (?, ?, ?)
               ON CONFLICT(twitch_login) DO UPDATE SET
                   twitch_user_id = COALESCE(excluded.twitch_user_id, chat_presence.twitch_user_id)""",
            (twitch_login, twitch_user_id, now),
        )
        giveaway = await self.active_giveaway()
        if giveaway is not None and count_time:
            await self._open_activity(giveaway.id, twitch_login, twitch_user_id, now)
        await self._db.commit()

    async def mark_parted(self, twitch_login: str) -> None:
        now = self.now()
        giveaway = await self.active_giveaway()
        if giveaway is not None:
            await self._close_activity(giveaway.id, twitch_login, now)
        await self._db.execute("DELETE FROM chat_presence WHERE twitch_login = ?", (twitch_login,))
        await self._db.commit()

    async def record_message(
        self,
        twitch_login: str,
        twitch_user_id: str | None,
        count_message: bool = True,
        count_time: bool = True,
    ) -> None:
        now = self.now()
        await self._db.execute(
            """INSERT INTO chat_presence(twitch_login, twitch_user_id, joined_at)
               VALUES (?, ?, ?)
               ON CONFLICT(twitch_login) DO UPDATE SET
                   twitch_user_id = COALESCE(excluded.twitch_user_id, chat_presence.twitch_user_id)""",
            (twitch_login, twitch_user_id, now),
        )
        giveaway = await self.active_giveaway()
        if giveaway is not None and count_time:
            await self._open_activity(giveaway.id, twitch_login, twitch_user_id, now)
            if count_message:
                await self._db.execute(
                    """UPDATE giveaway_activity
                       SET messages = messages + 1,
                           twitch_user_id = COALESCE(?, twitch_user_id),
                           last_counted_message_at = ?
                       WHERE giveaway_id = ? AND twitch_login = ? AND (
                           last_counted_message_at IS NULL OR ? - last_counted_message_at >= ?
                       )""",
                    (
                        twitch_user_id,
                        now,
                        giveaway.id,
                        twitch_login,
                        now,
                        giveaway.message_interval_seconds,
                    ),
                )
        await self._db.commit()

    async def start_giveaway(
        self,
        min_minutes: int,
        min_messages: int,
        winner_count: int = 1,
        title: str = "Розыгрыш",
        prize: str = "",
        count_existing_presence: bool = True,
        excluded_twitch_logins: tuple[str, ...] = (),
        message_interval_seconds: int = 0,
        min_participants: int = 1,
        end_at: int | None = None,
    ) -> Giveaway:
        title = title.strip() or "Розыгрыш"
        prize = prize.strip()
        if min_minutes < 1 or min_messages < 1 or winner_count < 1 or min_participants < 1:
            raise ValueError("Минимумы должны быть положительными числами.")
        if message_interval_seconds < 0:
            raise ValueError("Интервал между сообщениями не может быть отрицательным.")
        if winner_count > 100:
            raise ValueError("Количество победителей не должно быть больше 100.")
        if min_participants > 100_000:
            raise ValueError("Минимальное количество участников не должно быть больше 100000.")
        if len(title) > 120:
            raise ValueError("Название розыгрыша не должно быть длиннее 120 символов.")
        if len(prize) > 300:
            raise ValueError("Описание награды не должно быть длиннее 300 символов.")
        if await self.active_giveaway() is not None:
            raise ValueError("Сначала завершите текущий розыгрыш.")
        now = self.now()
        if end_at is not None and end_at <= now:
            raise ValueError("Дата и время завершения должны быть в будущем.")
        cursor = await self._db.execute(
            """INSERT INTO giveaways
               (state, title, title_key, prize, winner_count, min_participants,
                min_seconds, min_messages, message_interval_seconds, started_at, end_at)
               VALUES ('active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title,
                title.casefold(),
                prize,
                winner_count,
                min_participants,
                min_minutes * 60,
                min_messages,
                message_interval_seconds,
                now,
                end_at,
            ),
        )
        giveaway_id = int(cursor.lastrowid)
        if count_existing_presence:
            excluded_where = ""
            params: tuple[str, ...] = ()
            excluded = self._excluded_logins(excluded_twitch_logins)
            if excluded:
                excluded_where = (
                    " WHERE twitch_login NOT IN (" + ", ".join("?" for _ in excluded) + ")"
                )
                params = excluded
            present = await (
                await self._db.execute(
                    f"SELECT twitch_login, twitch_user_id FROM chat_presence{excluded_where}",
                    params,
                )
            ).fetchall()
            for person in present:
                await self._open_activity(
                    giveaway_id, str(person["twitch_login"]), person["twitch_user_id"], now
                )
        await self._db.commit()
        return Giveaway(
            giveaway_id,
            "active",
            title,
            prize,
            winner_count,
            min_participants,
            min_minutes * 60,
            min_messages,
            message_interval_seconds,
            now,
            None,
            None,
            end_at,
        )

    async def active_giveaway(self) -> Giveaway | None:
        row = await (
            await self._db.execute(
                """SELECT id, state, title, prize, winner_count, min_participants, min_seconds, min_messages, message_interval_seconds, started_at, finished_at, eligible_count_at_finish, end_at, twitch_announce_enabled, twitch_announce_interval_seconds, twitch_last_announce_at
                   FROM giveaways WHERE state = 'active' ORDER BY id DESC LIMIT 1"""
            )
        ).fetchone()
        return self._giveaway_from_row(row)

    async def latest_finished_giveaway(self) -> Giveaway | None:
        row = await (
            await self._db.execute(
                """SELECT id, state, title, prize, winner_count, min_participants, min_seconds, min_messages, message_interval_seconds, started_at, finished_at, eligible_count_at_finish, end_at, twitch_announce_enabled, twitch_announce_interval_seconds, twitch_last_announce_at
                   FROM giveaways WHERE state = 'finished' ORDER BY id DESC LIMIT 1"""
            )
        ).fetchone()
        return self._giveaway_from_row(row)

    async def latest_giveaway_by_title(self, title: str) -> Giveaway | None:
        row = await (
            await self._db.execute(
                """SELECT id, state, title, prize, winner_count, min_participants, min_seconds, min_messages, message_interval_seconds, started_at, finished_at, eligible_count_at_finish, end_at, twitch_announce_enabled, twitch_announce_interval_seconds, twitch_last_announce_at
                   FROM giveaways WHERE title_key = ? ORDER BY id DESC LIMIT 1""",
                (title.strip().casefold(),),
            )
        ).fetchone()
        return self._giveaway_from_row(row)

    async def latest_finished_giveaway_by_title(self, title: str) -> Giveaway | None:
        row = await (
            await self._db.execute(
                """SELECT id, state, title, prize, winner_count, min_participants, min_seconds, min_messages, message_interval_seconds, started_at, finished_at, eligible_count_at_finish, end_at, twitch_announce_enabled, twitch_announce_interval_seconds, twitch_last_announce_at
                   FROM giveaways WHERE state = 'finished' AND title_key = ? ORDER BY id DESC LIMIT 1""",
                (title.strip().casefold(),),
            )
        ).fetchone()
        return self._giveaway_from_row(row)

    @staticmethod
    def _giveaway_from_row(row: aiosqlite.Row | None) -> Giveaway | None:
        if row is None:
            return None
        return Giveaway(
            id=int(row["id"]),
            state=str(row["state"]),
            title=str(row["title"]),
            prize=str(row["prize"]),
            winner_count=int(row["winner_count"]),
            min_participants=int(row["min_participants"]),
            min_seconds=int(row["min_seconds"]),
            min_messages=int(row["min_messages"]),
            message_interval_seconds=int(row["message_interval_seconds"]),
            started_at=int(row["started_at"]),
            finished_at=int(row["finished_at"]) if row["finished_at"] else None,
            eligible_count_at_finish=(
                int(row["eligible_count_at_finish"])
                if row["eligible_count_at_finish"] is not None
                else None
            ),
            end_at=int(row["end_at"]) if row["end_at"] is not None else None,
            twitch_announce_enabled=bool(row["twitch_announce_enabled"]),
            twitch_announce_interval_seconds=int(
                row["twitch_announce_interval_seconds"]
            ),
            twitch_last_announce_at=(
                int(row["twitch_last_announce_at"])
                if row["twitch_last_announce_at"] is not None
                else None
            ),
        )

    async def finish_active_giveaway(
        self, eligible_count: int | None = None
    ) -> Giveaway | None:
        giveaway = await self.active_giveaway()
        if giveaway is None:
            return None
        now = self.now()
        rows = await (
            await self._db.execute(
                """SELECT twitch_login FROM giveaway_activity
                   WHERE giveaway_id = ? AND presence_started_at IS NOT NULL""",
                (giveaway.id,),
            )
        ).fetchall()
        for row in rows:
            await self._close_activity(giveaway.id, str(row["twitch_login"]), now)
        await self._db.execute(
            """UPDATE giveaways
               SET state = 'finished', finished_at = ?, eligible_count_at_finish = ?
               WHERE id = ?""",
            (now, eligible_count, giveaway.id),
        )
        await self._db.commit()
        return Giveaway(
            giveaway.id,
            "finished",
            giveaway.title,
            giveaway.prize,
            giveaway.winner_count,
            giveaway.min_participants,
            giveaway.min_seconds,
            giveaway.min_messages,
            giveaway.message_interval_seconds,
            giveaway.started_at,
            now,
            eligible_count,
            giveaway.end_at,
            giveaway.twitch_announce_enabled,
            giveaway.twitch_announce_interval_seconds,
            giveaway.twitch_last_announce_at,
        )

    async def configure_twitch_announcements(
        self, *, enabled: bool, interval_minutes: int | None = None
    ) -> Giveaway | None:
        giveaway = await self.active_giveaway()
        if giveaway is None:
            return None
        if interval_minutes is not None and not 1 <= interval_minutes <= 1440:
            raise ValueError("Интервал Twitch-анонсов должен быть от 1 до 1440 минут.")
        interval_seconds = (
            interval_minutes * 60
            if interval_minutes is not None
            else giveaway.twitch_announce_interval_seconds
        )
        last_announce_at = (
            giveaway.twitch_last_announce_at
            if enabled and giveaway.twitch_announce_enabled
            else None
        )
        await self._db.execute(
            """UPDATE giveaways
               SET twitch_announce_enabled = ?,
                   twitch_announce_interval_seconds = ?,
                   twitch_last_announce_at = ?
               WHERE id = ? AND state = 'active'""",
            (int(enabled), interval_seconds, last_announce_at, giveaway.id),
        )
        await self._db.commit()
        return await self.active_giveaway()

    async def mark_twitch_announcement_sent(
        self, giveaway_id: int, sent_at: int | None = None
    ) -> None:
        await self._db.execute(
            """UPDATE giveaways SET twitch_last_announce_at = ?
               WHERE id = ? AND state = 'active' AND twitch_announce_enabled = 1""",
            (self.now() if sent_at is None else sent_at, giveaway_id),
        )
        await self._db.commit()

    async def begin_live_tracking(
        self, excluded_twitch_logins: tuple[str, ...] = ()
    ) -> None:
        async with self._tracking_session_lock:
            await self._begin_live_tracking_unlocked(excluded_twitch_logins)

    async def _begin_live_tracking_unlocked(
        self, excluded_twitch_logins: tuple[str, ...] = ()
    ) -> None:
        giveaway = await self.active_giveaway()
        if giveaway is None:
            return
        now = self.now()
        excluded = self._excluded_logins(excluded_twitch_logins)
        excluded_where = ""
        params: tuple[str, ...] = ()
        if excluded:
            excluded_where = " WHERE twitch_login NOT IN (" + ", ".join("?" for _ in excluded) + ")"
            params = excluded
        present = await (
            await self._db.execute(
                f"SELECT twitch_login, twitch_user_id FROM chat_presence{excluded_where}",
                params,
            )
        ).fetchall()
        for person in present:
            await self._open_activity(
                giveaway.id, str(person["twitch_login"]), person["twitch_user_id"], now
            )
        await self._db.commit()

    async def end_live_tracking(self) -> None:
        async with self._tracking_session_lock:
            await self._end_live_tracking_unlocked()

    async def _end_live_tracking_unlocked(self) -> None:
        giveaway = await self.active_giveaway()
        if giveaway is None:
            return
        now = self.now()
        rows = await (
            await self._db.execute(
                """SELECT twitch_login FROM giveaway_activity
                   WHERE giveaway_id = ? AND presence_started_at IS NOT NULL""",
                (giveaway.id,),
            )
        ).fetchall()
        for row in rows:
            await self._close_activity(giveaway.id, str(row["twitch_login"]), now)
        await self._db.commit()

    async def reset_chat_session(self, *, preserve_elapsed: bool) -> None:
        """Close a finished IRC session and forget its now-stale presence list.

        A graceful disconnect preserves elapsed time. At process startup an open
        interval belongs to an interrupted old process, so it is discarded rather
        than counting the whole period while the service was unavailable.
        """
        async with self._tracking_session_lock:
            await self._reset_chat_session_unlocked(
                preserve_elapsed=preserve_elapsed
            )

    async def _reset_chat_session_unlocked(self, *, preserve_elapsed: bool) -> None:
        now = self.now()
        rows = await (
            await self._db.execute(
                """SELECT giveaway_id, twitch_login FROM giveaway_activity
                   WHERE presence_started_at IS NOT NULL"""
            )
        ).fetchall()
        if preserve_elapsed:
            for row in rows:
                await self._close_activity(
                    int(row["giveaway_id"]), str(row["twitch_login"]), now
                )
        elif rows:
            await self._db.execute(
                """UPDATE giveaway_activity SET presence_started_at = NULL
                   WHERE presence_started_at IS NOT NULL"""
            )
        await self._db.execute("DELETE FROM chat_presence")
        await self._db.commit()

    async def giveaway_status(
        self,
        giveaway: Giveaway,
        excluded_twitch_logins: tuple[str, ...] = (),
        excluded_telegram_usernames: tuple[str, ...] = (),
    ) -> tuple[int, int]:
        now = self.now()
        excluded_sql, excluded_params = self._registration_exclusions_sql(
            excluded_twitch_logins, excluded_telegram_usernames
        )
        row = await (
            await self._db.execute(
                """SELECT COUNT(*) AS tracked,
                   SUM(CASE WHEN COALESCE(activity.seconds, 0) +
                       CASE WHEN activity.presence_started_at IS NULL THEN 0
                           ELSE ? - activity.presence_started_at END >= ?
                       AND COALESCE(activity.messages, 0) >= ? THEN 1 ELSE 0 END) AS qualified
                   FROM giveaway_registrations AS registration
                   LEFT JOIN giveaway_activity AS activity
                     ON activity.giveaway_id = registration.giveaway_id
                    AND activity.twitch_login = registration.twitch_login
                   WHERE registration.giveaway_id = ?"""
                + excluded_sql,
                (
                    now,
                    giveaway.min_seconds,
                    giveaway.min_messages,
                    giveaway.id,
                    *excluded_params,
                ),
            )
        ).fetchone()
        return int(row["tracked"] or 0), int(row["qualified"] or 0)

    async def giveaway_participants(
        self,
        giveaway: Giveaway,
        excluded_twitch_logins: tuple[str, ...] = (),
        excluded_telegram_usernames: tuple[str, ...] = (),
    ) -> list[Participant]:
        now = self.now()
        excluded_sql, excluded_params = self._registration_exclusions_sql(
            excluded_twitch_logins, excluded_telegram_usernames
        )
        rows = await (
            await self._db.execute(
                """SELECT registration.twitch_login,
                   COALESCE(activity.seconds, 0) +
                       CASE WHEN activity.presence_started_at IS NULL THEN 0
                           ELSE ? - activity.presence_started_at END AS seconds,
                   COALESCE(activity.messages, 0) AS messages,
                   registration.telegram_user_id,
                   registration.telegram_name,
                   registration.telegram_username
                   FROM giveaway_registrations AS registration
                   LEFT JOIN giveaway_activity AS activity
                     ON activity.giveaway_id = registration.giveaway_id
                    AND activity.twitch_login = registration.twitch_login
                   WHERE registration.giveaway_id = ?
                """
                + excluded_sql
                + """
                   ORDER BY seconds DESC, messages DESC, registration.twitch_login COLLATE NOCASE""",
                (now, giveaway.id, *excluded_params),
            )
        ).fetchall()
        return [
            Participant(
                twitch_login=str(row["twitch_login"]),
                seconds=int(row["seconds"]),
                messages=int(row["messages"]),
                telegram_user_id=(
                    int(row["telegram_user_id"]) if row["telegram_user_id"] is not None else None
                ),
                telegram_name=(
                    str(row["telegram_name"]) if row["telegram_name"] is not None else None
                ),
                telegram_username=(
                    str(row["telegram_username"])
                    if row["telegram_username"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    async def participant_stats(
        self, giveaway: Giveaway, telegram_user_id: int
    ) -> Candidate | None:
        now = self.now()
        row = await (
            await self._db.execute(
                """SELECT registration.telegram_user_id,
                          registration.telegram_name,
                          registration.telegram_username,
                          registration.twitch_login,
                          COALESCE(activity.seconds, 0) +
                              CASE WHEN activity.presence_started_at IS NULL THEN 0
                                  ELSE ? - activity.presence_started_at END AS seconds,
                          COALESCE(activity.messages, 0) AS messages
                   FROM giveaway_registrations AS registration
                   LEFT JOIN giveaway_activity AS activity
                     ON activity.giveaway_id = registration.giveaway_id
                    AND activity.twitch_login = registration.twitch_login
                   WHERE registration.giveaway_id = ?
                     AND registration.telegram_user_id = ?""",
                (now, giveaway.id, telegram_user_id),
            )
        ).fetchone()
        if row is None:
            return None
        return Candidate(
            telegram_user_id=int(row["telegram_user_id"]),
            telegram_name=str(row["telegram_name"]),
            twitch_login=str(row["twitch_login"]),
            seconds=int(row["seconds"]),
            messages=int(row["messages"]),
            telegram_username=(
                str(row["telegram_username"])
                if row["telegram_username"] is not None
                else None
            ),
        )

    async def eligible_candidates(
        self,
        giveaway: Giveaway,
        excluded_twitch_logins: tuple[str, ...] = (),
        excluded_telegram_usernames: tuple[str, ...] = (),
    ) -> list[Candidate]:
        now = self.now()
        excluded_sql, excluded_params = self._registration_exclusions_sql(
            excluded_twitch_logins, excluded_telegram_usernames
        )
        rows = await (
            await self._db.execute(
                """SELECT registration.telegram_user_id, registration.telegram_name,
                   registration.telegram_username,
                   registration.twitch_login,
                   activity.seconds + CASE WHEN activity.presence_started_at IS NULL THEN 0
                       ELSE ? - activity.presence_started_at END AS seconds,
                   activity.messages
                   FROM giveaway_registrations AS registration
                   JOIN giveaway_activity AS activity
                     ON activity.giveaway_id = registration.giveaway_id
                    AND activity.twitch_login = registration.twitch_login
                   WHERE registration.giveaway_id = ? AND
                       activity.seconds + CASE WHEN activity.presence_started_at IS NULL THEN 0
                           ELSE ? - activity.presence_started_at END >= ? AND
                       activity.messages >= ? AND NOT EXISTS (
                           SELECT 1 FROM giveaway_winners AS winner
                           WHERE winner.giveaway_id = activity.giveaway_id
                             AND winner.telegram_user_id = registration.telegram_user_id
                       )"""
                + excluded_sql,
                (
                    now,
                    giveaway.id,
                    now,
                    giveaway.min_seconds,
                    giveaway.min_messages,
                    *excluded_params,
                ),
            )
        ).fetchall()
        return [
            Candidate(
                telegram_user_id=int(row["telegram_user_id"]),
                telegram_name=str(row["telegram_name"]),
                twitch_login=str(row["twitch_login"]),
                seconds=int(row["seconds"]),
                messages=int(row["messages"]),
                telegram_username=(
                    str(row["telegram_username"])
                    if row["telegram_username"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    async def record_winner(self, giveaway_id: int, telegram_user_id: int) -> None:
        row = await (
            await self._db.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 AS next_position FROM giveaway_winners WHERE giveaway_id = ?",
                (giveaway_id,),
            )
        ).fetchone()
        await self._db.execute(
            "INSERT INTO giveaway_winners VALUES (?, ?, ?, ?)",
            (giveaway_id, telegram_user_id, self.now(), int(row["next_position"])),
        )
        await self._db.commit()

    async def recorded_winners(self, giveaway: Giveaway) -> list[Candidate]:
        now = self.now()
        rows = await (
            await self._db.execute(
                """SELECT registration.telegram_user_id, registration.telegram_name,
                   registration.telegram_username,
                   registration.twitch_login,
                   activity.seconds + CASE WHEN activity.presence_started_at IS NULL THEN 0
                       ELSE ? - activity.presence_started_at END AS seconds,
                   activity.messages
                   FROM giveaway_winners AS winner
                   JOIN giveaway_registrations AS registration
                     ON registration.giveaway_id = winner.giveaway_id
                    AND registration.telegram_user_id = winner.telegram_user_id
                   JOIN giveaway_activity AS activity
                     ON activity.giveaway_id = winner.giveaway_id
                    AND activity.twitch_login = registration.twitch_login
                   WHERE winner.giveaway_id = ?
                   ORDER BY winner.position""",
                (now, giveaway.id),
            )
        ).fetchall()
        return [
            Candidate(
                telegram_user_id=int(row["telegram_user_id"]),
                telegram_name=str(row["telegram_name"]),
                twitch_login=str(row["twitch_login"]),
                seconds=int(row["seconds"]),
                messages=int(row["messages"]),
                telegram_username=(
                    str(row["telegram_username"])
                    if row["telegram_username"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    async def _open_activity(
        self, giveaway_id: int, twitch_login: str, twitch_user_id: str | None, now: int
    ) -> None:
        await self._db.execute(
            """INSERT INTO giveaway_activity
               (giveaway_id, twitch_login, twitch_user_id, presence_started_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(giveaway_id, twitch_login) DO UPDATE SET
                   twitch_user_id = COALESCE(excluded.twitch_user_id, giveaway_activity.twitch_user_id),
                   presence_started_at = COALESCE(giveaway_activity.presence_started_at, excluded.presence_started_at)""",
            (giveaway_id, twitch_login, twitch_user_id, now),
        )

    async def _close_activity(self, giveaway_id: int, twitch_login: str, now: int) -> None:
        await self._db.execute(
            """UPDATE giveaway_activity
               SET seconds = seconds + MAX(0, ? - presence_started_at), presence_started_at = NULL
               WHERE giveaway_id = ? AND twitch_login = ? AND presence_started_at IS NOT NULL""",
            (now, giveaway_id, twitch_login),
        )
