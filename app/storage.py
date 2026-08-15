from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
import secrets
import time

import aiosqlite


_UNSET = object()
SQLITE_MAX_INTEGER = (1 << 63) - 1
DRAW_SCORE_MAX = 999_999_999_999


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


@dataclass(frozen=True, slots=True)
class DrawEligibility:
    telegram_member: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DrawParticipant:
    telegram_user_id: int
    telegram_name: str
    telegram_username: str | None
    twitch_login: str
    twitch_user_id: str
    registered_at: int
    seconds: int
    messages: int
    time_requirement_met: bool
    message_requirement_met: bool
    excluded_by_twitch: bool
    excluded_by_telegram: bool
    eligibility_reason: str | None


@dataclass(frozen=True, slots=True)
class DrawRound:
    id: int
    giveaway_id: int
    round_number: int
    round_kind: str
    cutoff_at: int
    created_at: int
    forced: bool
    requested_winner_count: int
    registered_count: int
    eligible_count: int
    min_participants_required: int
    min_participants_met: bool
    title: str
    prize: str
    min_seconds: int
    min_messages: int


@dataclass(frozen=True, slots=True)
class DrawEntry:
    round_id: int
    giveaway_id: int
    telegram_user_id: int
    telegram_name: str
    telegram_username: str | None
    twitch_login: str
    twitch_user_id: str
    registered_at: int
    seconds: int
    messages: int
    time_requirement_met: bool
    message_requirement_met: bool
    excluded_by_twitch: bool
    excluded_by_telegram: bool
    telegram_member: bool
    previous_winner: bool
    final_eligible: bool
    eligibility_reason: str | None
    # Kept as a decimal string outside SQLite so report consumers preserve any
    # leading zeroes when displaying the fixed-width 12-digit draw value.
    random_score: str
    draw_rank: int | None
    winner_position: int | None


@dataclass(frozen=True, slots=True)
class DrawResult:
    round: DrawRound
    entries: tuple[DrawEntry, ...]
    winners: tuple[DrawEntry, ...]


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.db: aiosqlite.Connection | None = None
        self._link_code_lock = asyncio.Lock()
        self._tracking_session_lock = asyncio.Lock()
        self._giveaway_mutation_lock = asyncio.Lock()

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

            CREATE TABLE IF NOT EXISTS giveaway_draw_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                giveaway_id INTEGER NOT NULL REFERENCES giveaways(id),
                round_number INTEGER NOT NULL CHECK(round_number >= 1),
                round_kind TEXT NOT NULL CHECK(round_kind IN ('finish', 'reroll')),
                cutoff_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                forced INTEGER NOT NULL CHECK(forced IN (0, 1)),
                requested_winner_count INTEGER NOT NULL CHECK(requested_winner_count >= 1),
                registered_count INTEGER NOT NULL CHECK(registered_count >= 0),
                eligible_count INTEGER NOT NULL CHECK(eligible_count >= 0),
                min_participants_required INTEGER NOT NULL CHECK(min_participants_required >= 1),
                min_participants_met INTEGER NOT NULL CHECK(min_participants_met IN (0, 1)),
                title TEXT NOT NULL,
                prize TEXT NOT NULL,
                min_seconds INTEGER NOT NULL CHECK(min_seconds >= 0),
                min_messages INTEGER NOT NULL CHECK(min_messages >= 0),
                UNIQUE (giveaway_id, round_number)
            );

            CREATE TABLE IF NOT EXISTS giveaway_draw_entries (
                round_id INTEGER NOT NULL REFERENCES giveaway_draw_rounds(id),
                giveaway_id INTEGER NOT NULL REFERENCES giveaways(id),
                telegram_user_id INTEGER NOT NULL,
                telegram_name TEXT NOT NULL,
                telegram_username TEXT,
                twitch_login TEXT NOT NULL,
                twitch_user_id TEXT NOT NULL,
                registered_at INTEGER NOT NULL,
                seconds INTEGER NOT NULL CHECK(seconds >= 0),
                messages INTEGER NOT NULL CHECK(messages >= 0),
                time_requirement_met INTEGER NOT NULL CHECK(time_requirement_met IN (0, 1)),
                message_requirement_met INTEGER NOT NULL CHECK(message_requirement_met IN (0, 1)),
                excluded_by_twitch INTEGER NOT NULL CHECK(excluded_by_twitch IN (0, 1)),
                excluded_by_telegram INTEGER NOT NULL CHECK(excluded_by_telegram IN (0, 1)),
                telegram_member INTEGER NOT NULL CHECK(telegram_member IN (0, 1)),
                previous_winner INTEGER NOT NULL CHECK(previous_winner IN (0, 1)),
                final_eligible INTEGER NOT NULL CHECK(final_eligible IN (0, 1)),
                eligibility_reason TEXT,
                random_score INTEGER NOT NULL CHECK(random_score >= 0),
                draw_rank INTEGER CHECK(draw_rank >= 1),
                winner_position INTEGER CHECK(winner_position >= 1),
                PRIMARY KEY (round_id, telegram_user_id),
                UNIQUE (round_id, twitch_login),
                UNIQUE (round_id, random_score),
                UNIQUE (round_id, winner_position)
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
        # Registration and finishing must be serialized. Otherwise a claim that
        # started just before the draw could insert a registration after the
        # immutable draw snapshot had already been committed.
        async with self._giveaway_mutation_lock:
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
        # A dedicated transaction prevents unrelated handlers using ``self._db``
        # from committing a half-finished link operation. BEGIN IMMEDIATE also
        # makes the active-state check and registration insertion atomic against
        # another process that may be finishing the same giveaway.
        claim_db = await aiosqlite.connect(self.path, timeout=30)
        claim_db.row_factory = aiosqlite.Row
        try:
            await claim_db.execute("PRAGMA foreign_keys = ON")
            await claim_db.execute("BEGIN IMMEDIATE")
            result = await self._claim_link_code_transaction(
                claim_db,
                code,
                twitch_login,
                twitch_user_id,
            )
            await claim_db.commit()
            return result
        except Exception:
            await claim_db.rollback()
            raise
        finally:
            await claim_db.close()

    async def _claim_link_code_transaction(
        self,
        claim_db: aiosqlite.Connection,
        code: str,
        twitch_login: str,
        twitch_user_id: str,
    ) -> tuple[str, int | None, str | None]:
        now = self.now()
        row = await (
            await claim_db.execute(
                """SELECT telegram_user_id, telegram_name, expires_at, giveaway_id,
                          telegram_username
                   FROM link_codes WHERE code = ?""",
                (code.upper(),),
            )
        ).fetchone()
        if row is None:
            return "missing", None, None
        if row["expires_at"] < now:
            await claim_db.execute(
                "DELETE FROM link_codes WHERE code = ?", (code.upper(),)
            )
            return "expired", None, None

        giveaway_id = int(row["giveaway_id"]) if row["giveaway_id"] is not None else None
        active_row = None
        if giveaway_id is not None:
            active_row = await (
                await claim_db.execute(
                    "SELECT id FROM giveaways WHERE id = ? AND state = 'active'",
                    (giveaway_id,),
                )
            ).fetchone()
        if active_row is None:
            await claim_db.execute(
                "DELETE FROM link_codes WHERE code = ?", (code.upper(),)
            )
            return "giveaway_closed", None, None

        existing = await (
            await claim_db.execute(
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

        await claim_db.execute(
            "DELETE FROM twitch_links WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        await claim_db.execute(
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
        await claim_db.execute(
            """DELETE FROM giveaway_registrations
               WHERE giveaway_id = ? AND telegram_user_id = ?""",
            (giveaway_id, telegram_user_id),
        )
        await claim_db.execute(
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
                giveaway_id,
                twitch_login,
                twitch_user_id,
                telegram_user_id,
                telegram_name,
                now,
                telegram_username,
            ),
        )
        await claim_db.execute(
            "DELETE FROM link_codes WHERE code = ?", (code.upper(),)
        )
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
        if min_minutes > SQLITE_MAX_INTEGER // 60:
            raise ValueError("Указано слишком большое количество минут.")
        if min_messages > SQLITE_MAX_INTEGER:
            raise ValueError("Указано слишком большое количество сообщений.")
        if message_interval_seconds > SQLITE_MAX_INTEGER:
            raise ValueError("Указан слишком большой интервал между сообщениями.")
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

    async def update_active_giveaway(
        self, **changes: object
    ) -> Giveaway | None:
        async with self._giveaway_mutation_lock:
            return await self._update_active_giveaway_unlocked(**changes)

    async def _update_active_giveaway_unlocked(
        self,
        *,
        min_minutes: int | object = _UNSET,
        min_messages: int | object = _UNSET,
        winner_count: int | object = _UNSET,
        message_interval_seconds: int | object = _UNSET,
        min_participants: int | object = _UNSET,
        end_at: int | None | object = _UNSET,
        title: str | object = _UNSET,
        prize: str | object = _UNSET,
    ) -> Giveaway | None:
        supplied = {
            "min_minutes": min_minutes,
            "min_messages": min_messages,
            "winner_count": winner_count,
            "message_interval_seconds": message_interval_seconds,
            "min_participants": min_participants,
            "end_at": end_at,
            "title": title,
            "prize": prize,
        }
        if all(value is _UNSET for value in supplied.values()):
            raise ValueError("Не указаны параметры для изменения.")

        giveaway = await self.active_giveaway()
        if giveaway is None:
            return None

        set_parts: list[str] = []
        values: list[int | str | None] = []

        if min_minutes is not _UNSET:
            if not isinstance(min_minutes, int) or isinstance(min_minutes, bool):
                raise ValueError("Минимальное время должно быть целым числом минут.")
            if min_minutes < 1:
                raise ValueError("Минимальное время должно быть не меньше 1 минуты.")
            if min_minutes > SQLITE_MAX_INTEGER // 60:
                raise ValueError("Указано слишком большое количество минут.")
            set_parts.append("min_seconds = ?")
            values.append(min_minutes * 60)

        if min_messages is not _UNSET:
            if not isinstance(min_messages, int) or isinstance(min_messages, bool):
                raise ValueError("Минимальное количество сообщений должно быть целым числом.")
            if min_messages < 1:
                raise ValueError("Минимальное количество сообщений должно быть не меньше 1.")
            if min_messages > SQLITE_MAX_INTEGER:
                raise ValueError("Указано слишком большое количество сообщений.")
            set_parts.append("min_messages = ?")
            values.append(min_messages)

        if winner_count is not _UNSET:
            if not isinstance(winner_count, int) or isinstance(winner_count, bool):
                raise ValueError("Количество победителей должно быть целым числом.")
            if not 1 <= winner_count <= 100:
                raise ValueError("Количество победителей должно быть от 1 до 100.")
            set_parts.append("winner_count = ?")
            values.append(winner_count)

        if message_interval_seconds is not _UNSET:
            if not isinstance(message_interval_seconds, int) or isinstance(
                message_interval_seconds, bool
            ):
                raise ValueError("Интервал между сообщениями должен быть целым числом секунд.")
            if message_interval_seconds < 0:
                raise ValueError("Интервал между сообщениями не может быть отрицательным.")
            if message_interval_seconds > SQLITE_MAX_INTEGER:
                raise ValueError("Указан слишком большой интервал между сообщениями.")
            set_parts.append("message_interval_seconds = ?")
            values.append(message_interval_seconds)

        if min_participants is not _UNSET:
            if not isinstance(min_participants, int) or isinstance(min_participants, bool):
                raise ValueError("Минимальное количество участников должно быть целым числом.")
            if not 1 <= min_participants <= 100_000:
                raise ValueError(
                    "Минимальное количество участников должно быть от 1 до 100000."
                )
            set_parts.append("min_participants = ?")
            values.append(min_participants)

        if end_at is not _UNSET:
            if end_at is not None and (
                not isinstance(end_at, int) or isinstance(end_at, bool)
            ):
                raise ValueError("Дата завершения должна быть указана целым timestamp.")
            if end_at is not None and end_at <= self.now():
                raise ValueError("Дата и время завершения должны быть в будущем.")
            set_parts.append("end_at = ?")
            values.append(end_at)

        if title is not _UNSET:
            if not isinstance(title, str):
                raise ValueError("Название розыгрыша должно быть текстом.")
            normalized_title = title.strip()
            if not normalized_title:
                raise ValueError("Название розыгрыша не может быть пустым.")
            if len(normalized_title) > 120:
                raise ValueError("Название розыгрыша не должно быть длиннее 120 символов.")
            set_parts.extend(("title = ?", "title_key = ?"))
            values.extend((normalized_title, normalized_title.casefold()))

        if prize is not _UNSET:
            if not isinstance(prize, str):
                raise ValueError("Описание награды должно быть текстом.")
            normalized_prize = prize.strip()
            if len(normalized_prize) > 300:
                raise ValueError("Описание награды не должно быть длиннее 300 символов.")
            set_parts.append("prize = ?")
            values.append(normalized_prize)

        try:
            cursor = await self._db.execute(
                f"""UPDATE giveaways SET {', '.join(set_parts)}
                    WHERE id = ? AND state = 'active'""",
                (*values, giveaway.id),
            )
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise
        if cursor.rowcount == 0:
            return None
        row = await (
            await self._db.execute(
                """SELECT id, state, title, prize, winner_count, min_participants, min_seconds, min_messages, message_interval_seconds, started_at, finished_at, eligible_count_at_finish, end_at, twitch_announce_enabled, twitch_announce_interval_seconds, twitch_last_announce_at
                   FROM giveaways WHERE id = ? AND state = 'active'""",
                (giveaway.id,),
            )
        ).fetchone()
        return self._giveaway_from_row(row)

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
        self,
        eligible_count: int | None = None,
        *,
        expected_giveaway: Giveaway | None = None,
    ) -> Giveaway | None:
        async with self._giveaway_mutation_lock:
            return await self._finish_active_giveaway_unlocked(
                eligible_count, expected_giveaway=expected_giveaway
            )

    async def _finish_active_giveaway_unlocked(
        self,
        eligible_count: int | None = None,
        *,
        expected_giveaway: Giveaway | None = None,
    ) -> Giveaway | None:
        giveaway = await self.active_giveaway()
        if giveaway is None:
            return None
        if expected_giveaway is not None and expected_giveaway.id != giveaway.id:
            return None
        now = self.now()
        expected_where = ""
        update_values: list[int | str | None] = [now, eligible_count, giveaway.id]
        if expected_giveaway is not None:
            expected_where = (
                " AND title = ? AND prize = ? AND winner_count = ?"
                " AND min_participants = ? AND min_seconds = ? AND min_messages = ?"
                " AND message_interval_seconds = ? AND end_at IS ?"
            )
            update_values.extend(
                (
                    expected_giveaway.title,
                    expected_giveaway.prize,
                    expected_giveaway.winner_count,
                    expected_giveaway.min_participants,
                    expected_giveaway.min_seconds,
                    expected_giveaway.min_messages,
                    expected_giveaway.message_interval_seconds,
                    expected_giveaway.end_at,
                )
            )
        try:
            cursor = await self._db.execute(
                """UPDATE giveaways
                   SET state = 'finished', finished_at = ?, eligible_count_at_finish = ?,
                       twitch_announce_enabled = 0, twitch_last_announce_at = NULL
                   WHERE id = ? AND state = 'active'"""
                + expected_where,
                tuple(update_values),
            )
            if cursor.rowcount == 0:
                await self._db.commit()
                return None
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
        except Exception:
            await self._db.rollback()
            raise
        row = await (
            await self._db.execute(
                """SELECT id, state, title, prize, winner_count, min_participants, min_seconds, min_messages, message_interval_seconds, started_at, finished_at, eligible_count_at_finish, end_at, twitch_announce_enabled, twitch_announce_interval_seconds, twitch_last_announce_at
                   FROM giveaways WHERE id = ? AND state = 'finished'""",
                (giveaway.id,),
            )
        ).fetchone()
        return self._giveaway_from_row(row)

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

    async def draw_participants(
        self,
        giveaway: Giveaway,
        excluded_twitch_logins: tuple[str, ...] = (),
        excluded_telegram_usernames: tuple[str, ...] = (),
    ) -> list[DrawParticipant]:
        """Return every registration for membership checks before a draw.

        This is only a preview. ``create_draw_round`` takes a fresh statistics
        snapshot at its cutoff and refuses the draw if the registration set has
        changed while Telegram membership was being checked.
        """

        return await self._draw_participants_at(
            giveaway,
            self.now(),
            excluded_twitch_logins,
            excluded_telegram_usernames,
        )

    async def _draw_participants_at(
        self,
        giveaway: Giveaway,
        cutoff_at: int,
        excluded_twitch_logins: tuple[str, ...],
        excluded_telegram_usernames: tuple[str, ...],
        *,
        db: aiosqlite.Connection | None = None,
    ) -> list[DrawParticipant]:
        connection = db or self._db
        rows = await (
            await connection.execute(
                """SELECT registration.telegram_user_id,
                          registration.telegram_name,
                          registration.telegram_username,
                          registration.twitch_login,
                          registration.twitch_user_id,
                          registration.registered_at,
                          COALESCE(activity.seconds, 0) +
                              CASE WHEN activity.presence_started_at IS NULL THEN 0
                                   ELSE MAX(0, ? - activity.presence_started_at) END
                              AS seconds,
                          COALESCE(activity.messages, 0) AS messages
                   FROM giveaway_registrations AS registration
                   LEFT JOIN giveaway_activity AS activity
                     ON activity.giveaway_id = registration.giveaway_id
                    AND activity.twitch_login = registration.twitch_login
                   WHERE registration.giveaway_id = ?
                   ORDER BY registration.registered_at,
                            registration.telegram_user_id""",
                (cutoff_at, giveaway.id),
            )
        ).fetchall()
        excluded_twitch = set(self._excluded_logins(excluded_twitch_logins))
        excluded_telegram = set(
            self._excluded_telegram_usernames(excluded_telegram_usernames)
        )
        participants: list[DrawParticipant] = []
        for row in rows:
            twitch_login = str(row["twitch_login"])
            telegram_username = (
                str(row["telegram_username"])
                if row["telegram_username"] is not None
                else None
            )
            seconds = max(0, int(row["seconds"]))
            messages = max(0, int(row["messages"]))
            time_requirement_met = seconds >= giveaway.min_seconds
            message_requirement_met = messages >= giveaway.min_messages
            excluded_by_twitch = (
                twitch_login.strip().casefold().lstrip("@") in excluded_twitch
            )
            excluded_by_telegram = (
                telegram_username is not None
                and telegram_username.strip().casefold().lstrip("@")
                in excluded_telegram
            )
            reasons: list[str] = []
            if not time_requirement_met:
                reasons.append("not_enough_time")
            if not message_requirement_met:
                reasons.append("not_enough_messages")
            if excluded_by_twitch:
                reasons.append("excluded_twitch")
            if excluded_by_telegram:
                reasons.append("excluded_telegram")
            participants.append(
                DrawParticipant(
                    telegram_user_id=int(row["telegram_user_id"]),
                    telegram_name=str(row["telegram_name"]),
                    telegram_username=telegram_username,
                    twitch_login=twitch_login,
                    twitch_user_id=str(row["twitch_user_id"]),
                    registered_at=int(row["registered_at"]),
                    seconds=seconds,
                    messages=messages,
                    time_requirement_met=time_requirement_met,
                    message_requirement_met=message_requirement_met,
                    excluded_by_twitch=excluded_by_twitch,
                    excluded_by_telegram=excluded_by_telegram,
                    eligibility_reason=",".join(reasons) or None,
                )
            )
        return participants

    async def create_draw_round(
        self,
        giveaway: Giveaway,
        eligibility_by_telegram_id: Mapping[int, DrawEligibility | bool],
        *,
        reroll: bool = False,
        force: bool = False,
        excluded_twitch_logins: tuple[str, ...] = (),
        excluded_telegram_usernames: tuple[str, ...] = (),
        score_factory: Callable[[], int] | None = None,
        finish: bool = True,
    ) -> DrawResult:
        """Persist one immutable draw and its winners in one transaction.

        The first round snapshots and, by default, finishes the active giveaway.
        A reroll always selects one new winner and excludes every winner already
        recorded for the giveaway. Scores are unique within a round and are saved
        for all registrations, including registrations that are not eligible.
        """

        if reroll and finish:
            # A reroll operates on an already finished giveaway.
            finish = False
        normalized_eligibility = self._normalize_draw_eligibility(
            eligibility_by_telegram_id
        )
        async with self._giveaway_mutation_lock:
            return await self._create_draw_round_unlocked(
                giveaway,
                normalized_eligibility,
                reroll=reroll,
                force=force,
                excluded_twitch_logins=excluded_twitch_logins,
                excluded_telegram_usernames=excluded_telegram_usernames,
                score_factory=score_factory,
                finish=finish,
            )

    @staticmethod
    def _normalize_draw_eligibility(
        eligibility_by_telegram_id: Mapping[int, DrawEligibility | bool],
    ) -> dict[int, DrawEligibility]:
        normalized: dict[int, DrawEligibility] = {}
        for telegram_user_id, value in eligibility_by_telegram_id.items():
            if not isinstance(telegram_user_id, int) or isinstance(
                telegram_user_id, bool
            ):
                raise ValueError("Telegram ID участника должен быть целым числом.")
            decision = DrawEligibility(value) if isinstance(value, bool) else value
            if not isinstance(decision, DrawEligibility) or not isinstance(
                decision.telegram_member, bool
            ):
                raise ValueError("Для каждого участника нужен результат проверки Telegram.")
            reason = decision.reason.strip() if decision.reason else None
            if reason is not None and len(reason) > 500:
                raise ValueError("Причина недопуска не должна быть длиннее 500 символов.")
            normalized[telegram_user_id] = DrawEligibility(
                telegram_member=decision.telegram_member,
                reason=reason,
            )
        return normalized

    async def _create_draw_round_unlocked(
        self,
        giveaway: Giveaway,
        eligibility_by_telegram_id: dict[int, DrawEligibility],
        *,
        reroll: bool,
        force: bool,
        excluded_twitch_logins: tuple[str, ...],
        excluded_telegram_usernames: tuple[str, ...],
        score_factory: Callable[[], int] | None,
        finish: bool,
    ) -> DrawResult:
        # A dedicated connection is essential here. Other handlers commit on
        # ``self._db``; sharing that connection would let (for example) a chat
        # message accidentally commit a half-written draw transaction.
        draw_db = await aiosqlite.connect(self.path, timeout=30)
        draw_db.row_factory = aiosqlite.Row
        try:
            await draw_db.execute("PRAGMA foreign_keys = ON")
            await draw_db.execute("BEGIN IMMEDIATE")
            row = await (
                await draw_db.execute(
                    """SELECT id, state, title, prize, winner_count, min_participants,
                              min_seconds, min_messages, message_interval_seconds,
                              started_at, finished_at, eligible_count_at_finish, end_at,
                              twitch_announce_enabled,
                              twitch_announce_interval_seconds,
                              twitch_last_announce_at
                       FROM giveaways WHERE id = ?""",
                    (giveaway.id,),
                )
            ).fetchone()
            current = self._giveaway_from_row(row)
            if current is None:
                raise ValueError("Розыгрыш не найден.")

            if not reroll:
                existing = await (
                    await draw_db.execute(
                        """SELECT id FROM giveaway_draw_rounds
                           WHERE giveaway_id = ? AND round_kind = 'finish'
                           ORDER BY round_number LIMIT 1""",
                        (giveaway.id,),
                    )
                ).fetchone()
                if existing is not None:
                    if finish and current.state == "active":
                        raise RuntimeError(
                            "Снимок жеребьёвки уже создан, но розыгрыш ещё активен."
                        )
                    result = await self._draw_result_for_round_unlocked(
                        int(existing["id"]), db=draw_db
                    )
                    await draw_db.commit()
                    return result
                other_round = await (
                    await draw_db.execute(
                        """SELECT 1 FROM giveaway_draw_rounds
                           WHERE giveaway_id = ? LIMIT 1""",
                        (giveaway.id,),
                    )
                ).fetchone()
                if other_round is not None:
                    raise ValueError(
                        "Нельзя создать первую жеребьёвку после сохранённого reroll."
                    )
                if current.state != "active":
                    raise ValueError("Розыгрыш уже завершён без снимка жеребьёвки.")
                if not self._same_draw_rules(current, giveaway):
                    raise ValueError(
                        "Параметры розыгрыша изменились; повторите проверку участников."
                    )
            elif current.state != "finished":
                raise ValueError("Повторная жеребьёвка доступна только после завершения.")

            cutoff_at = self.now()
            if finish:
                await draw_db.execute(
                    """UPDATE giveaway_activity
                       SET seconds = seconds + MAX(0, ? - presence_started_at),
                           presence_started_at = NULL
                       WHERE giveaway_id = ? AND presence_started_at IS NOT NULL""",
                    (cutoff_at, giveaway.id),
                )

            participants = await self._draw_participants_at(
                current,
                cutoff_at,
                excluded_twitch_logins,
                excluded_telegram_usernames,
                db=draw_db,
            )
            registration_ids = {
                participant.telegram_user_id for participant in participants
            }
            decision_ids = set(eligibility_by_telegram_id)
            if decision_ids != registration_ids:
                missing = sorted(registration_ids - decision_ids)
                extra = sorted(decision_ids - registration_ids)
                details: list[str] = []
                if missing:
                    details.append("не проверены: " + ", ".join(map(str, missing)))
                if extra:
                    details.append("лишние: " + ", ".join(map(str, extra)))
                raise ValueError(
                    "Список регистраций изменился после проверки Telegram ("
                    + "; ".join(details)
                    + ")."
                )

            winner_rows = await (
                await draw_db.execute(
                    """SELECT telegram_user_id FROM giveaway_winners
                       WHERE giveaway_id = ?""",
                    (giveaway.id,),
                )
            ).fetchall()
            previous_winner_ids = {
                int(winner_row["telegram_user_id"]) for winner_row in winner_rows
            }
            if reroll and not previous_winner_ids:
                raise ValueError(
                    "Повторная жеребьёвка доступна только после выбора хотя бы "
                    "одного победителя в основном раунде."
                )
            if not reroll and previous_winner_ids:
                raise ValueError(
                    "Для розыгрыша уже записаны победители без снимка жеребьёвки."
                )

            scores = self._unique_draw_scores(len(participants), score_factory)
            prepared: list[dict[str, object]] = []
            for participant, random_score in zip(participants, scores, strict=True):
                membership = eligibility_by_telegram_id[
                    participant.telegram_user_id
                ]
                reasons: list[str] = []
                if not participant.time_requirement_met:
                    reasons.append("not_enough_time")
                if not participant.message_requirement_met:
                    reasons.append("not_enough_messages")
                if participant.excluded_by_twitch:
                    reasons.append("excluded_twitch")
                if participant.excluded_by_telegram:
                    reasons.append("excluded_telegram")
                if not membership.telegram_member:
                    reasons.append(membership.reason or "telegram_not_member")
                previous_winner = (
                    reroll
                    and participant.telegram_user_id in previous_winner_ids
                )
                if previous_winner:
                    reasons.append("previous_winner")
                prepared.append(
                    {
                        "participant": participant,
                        "telegram_member": membership.telegram_member,
                        "previous_winner": previous_winner,
                        "final_eligible": not reasons,
                        "eligibility_reason": ",".join(reasons) or None,
                        "random_score": random_score,
                        "draw_rank": None,
                        "winner_position": None,
                    }
                )

            eligible = sorted(
                (item for item in prepared if bool(item["final_eligible"])),
                key=lambda item: int(item["random_score"]),
                reverse=True,
            )
            if reroll and not eligible:
                raise ValueError("Для повторной жеребьёвки нет допущенных участников.")
            for rank, item in enumerate(eligible, 1):
                item["draw_rank"] = rank

            requested_winner_count = 1 if reroll else current.winner_count
            min_participants_required = 1 if reroll else current.min_participants
            min_participants_met = len(eligible) >= min_participants_required
            selected = (
                eligible[:requested_winner_count]
                if force or min_participants_met
                else []
            )
            position_row = await (
                await draw_db.execute(
                    """SELECT COALESCE(MAX(position), 0) AS last_position
                       FROM giveaway_winners WHERE giveaway_id = ?""",
                    (giveaway.id,),
                )
            ).fetchone()
            first_winner_position = int(position_row["last_position"]) + 1
            for offset, item in enumerate(selected):
                item["winner_position"] = first_winner_position + offset

            number_row = await (
                await draw_db.execute(
                    """SELECT COALESCE(MAX(round_number), 0) + 1 AS next_number
                       FROM giveaway_draw_rounds WHERE giveaway_id = ?""",
                    (giveaway.id,),
                )
            ).fetchone()
            round_number = int(number_row["next_number"])
            cursor = await draw_db.execute(
                """INSERT INTO giveaway_draw_rounds
                   (giveaway_id, round_number, round_kind, cutoff_at, created_at,
                    forced, requested_winner_count, registered_count,
                    eligible_count, min_participants_required,
                    min_participants_met, title, prize, min_seconds, min_messages)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    giveaway.id,
                    round_number,
                    "reroll" if reroll else "finish",
                    cutoff_at,
                    cutoff_at,
                    int(force),
                    requested_winner_count,
                    len(prepared),
                    len(eligible),
                    min_participants_required,
                    int(min_participants_met),
                    current.title,
                    current.prize,
                    current.min_seconds,
                    current.min_messages,
                ),
            )
            round_id = int(cursor.lastrowid)
            await draw_db.executemany(
                """INSERT INTO giveaway_draw_entries
                   (round_id, giveaway_id, telegram_user_id, telegram_name,
                    telegram_username, twitch_login, twitch_user_id, registered_at,
                    seconds, messages, time_requirement_met,
                    message_requirement_met, excluded_by_twitch,
                    excluded_by_telegram, telegram_member, previous_winner,
                    final_eligible, eligibility_reason, random_score, draw_rank,
                    winner_position)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        round_id,
                        giveaway.id,
                        participant.telegram_user_id,
                        participant.telegram_name,
                        participant.telegram_username,
                        participant.twitch_login,
                        participant.twitch_user_id,
                        participant.registered_at,
                        participant.seconds,
                        participant.messages,
                        int(participant.time_requirement_met),
                        int(participant.message_requirement_met),
                        int(participant.excluded_by_twitch),
                        int(participant.excluded_by_telegram),
                        int(bool(item["telegram_member"])),
                        int(bool(item["previous_winner"])),
                        int(bool(item["final_eligible"])),
                        item["eligibility_reason"],
                        int(item["random_score"]),
                        item["draw_rank"],
                        item["winner_position"],
                    )
                    for item in prepared
                    for participant in (item["participant"],)
                ],
            )
            if selected:
                await draw_db.executemany(
                    """INSERT INTO giveaway_winners
                       (giveaway_id, telegram_user_id, drawn_at, position)
                       VALUES (?, ?, ?, ?)""",
                    [
                        (
                            giveaway.id,
                            item["participant"].telegram_user_id,
                            cutoff_at,
                            item["winner_position"],
                        )
                        for item in selected
                    ],
                )
            if finish:
                finish_cursor = await draw_db.execute(
                    """UPDATE giveaways
                       SET state = 'finished', finished_at = ?,
                           eligible_count_at_finish = ?,
                           twitch_announce_enabled = 0,
                           twitch_last_announce_at = NULL
                       WHERE id = ? AND state = 'active'""",
                    (cutoff_at, len(eligible), giveaway.id),
                )
                if finish_cursor.rowcount != 1:
                    raise RuntimeError("Не удалось атомарно завершить розыгрыш.")
            await draw_db.commit()
            return await self._draw_result_for_round_unlocked(round_id, db=draw_db)
        except Exception:
            await draw_db.rollback()
            raise
        finally:
            await draw_db.close()

    @staticmethod
    def _same_draw_rules(current: Giveaway, expected: Giveaway) -> bool:
        return (
            current.id,
            current.title,
            current.prize,
            current.winner_count,
            current.min_participants,
            current.min_seconds,
            current.min_messages,
            current.message_interval_seconds,
            current.end_at,
        ) == (
            expected.id,
            expected.title,
            expected.prize,
            expected.winner_count,
            expected.min_participants,
            expected.min_seconds,
            expected.min_messages,
            expected.message_interval_seconds,
            expected.end_at,
        )

    @staticmethod
    def _unique_draw_scores(
        count: int, score_factory: Callable[[], int] | None
    ) -> list[int]:
        factory = score_factory or (lambda: secrets.randbelow(DRAW_SCORE_MAX + 1))
        scores: list[int] = []
        seen: set[int] = set()
        max_attempts = max(1_000, count * 100)
        attempts = 0
        while len(scores) < count:
            attempts += 1
            if attempts > max_attempts:
                raise RuntimeError("Не удалось сгенерировать уникальные случайные баллы.")
            score = factory()
            if not isinstance(score, int) or isinstance(score, bool):
                raise ValueError("Случайный балл должен быть целым числом.")
            if not 0 <= score <= DRAW_SCORE_MAX:
                raise ValueError("Случайный балл должен содержать не более 12 цифр.")
            if score in seen:
                continue
            seen.add(score)
            scores.append(score)
        return scores

    async def draw_rounds(self, giveaway: Giveaway | int) -> list[DrawRound]:
        giveaway_id = giveaway.id if isinstance(giveaway, Giveaway) else giveaway
        rows = await (
            await self._db.execute(
                """SELECT * FROM giveaway_draw_rounds
                   WHERE giveaway_id = ? ORDER BY round_number""",
                (giveaway_id,),
            )
        ).fetchall()
        return [self._draw_round_from_row(row) for row in rows]

    async def draw_entries(self, round_id: int) -> list[DrawEntry]:
        return list(await self._draw_entries_for_round_unlocked(round_id))

    async def latest_draw_result(
        self, giveaway: Giveaway | int
    ) -> DrawResult | None:
        giveaway_id = giveaway.id if isinstance(giveaway, Giveaway) else giveaway
        row = await (
            await self._db.execute(
                """SELECT id FROM giveaway_draw_rounds
                   WHERE giveaway_id = ? ORDER BY round_number DESC LIMIT 1""",
                (giveaway_id,),
            )
        ).fetchone()
        if row is None:
            return None
        return await self._draw_result_for_round_unlocked(int(row["id"]))

    async def _draw_result_for_round_unlocked(
        self, round_id: int, *, db: aiosqlite.Connection | None = None
    ) -> DrawResult:
        connection = db or self._db
        row = await (
            await connection.execute(
                "SELECT * FROM giveaway_draw_rounds WHERE id = ?", (round_id,)
            )
        ).fetchone()
        if row is None:
            raise ValueError("Раунд жеребьёвки не найден.")
        draw_round = self._draw_round_from_row(row)
        entries = await self._draw_entries_for_round_unlocked(round_id, db=connection)
        winners = tuple(
            sorted(
                (entry for entry in entries if entry.winner_position is not None),
                key=lambda entry: int(entry.winner_position or 0),
            )
        )
        return DrawResult(round=draw_round, entries=entries, winners=winners)

    async def _draw_entries_for_round_unlocked(
        self, round_id: int, *, db: aiosqlite.Connection | None = None
    ) -> tuple[DrawEntry, ...]:
        connection = db or self._db
        rows = await (
            await connection.execute(
                """SELECT * FROM giveaway_draw_entries WHERE round_id = ?
                   ORDER BY CASE WHEN draw_rank IS NULL THEN 1 ELSE 0 END,
                            draw_rank, telegram_user_id""",
                (round_id,),
            )
        ).fetchall()
        return tuple(self._draw_entry_from_row(row) for row in rows)

    @staticmethod
    def _draw_round_from_row(row: aiosqlite.Row) -> DrawRound:
        return DrawRound(
            id=int(row["id"]),
            giveaway_id=int(row["giveaway_id"]),
            round_number=int(row["round_number"]),
            round_kind=str(row["round_kind"]),
            cutoff_at=int(row["cutoff_at"]),
            created_at=int(row["created_at"]),
            forced=bool(row["forced"]),
            requested_winner_count=int(row["requested_winner_count"]),
            registered_count=int(row["registered_count"]),
            eligible_count=int(row["eligible_count"]),
            min_participants_required=int(row["min_participants_required"]),
            min_participants_met=bool(row["min_participants_met"]),
            title=str(row["title"]),
            prize=str(row["prize"]),
            min_seconds=int(row["min_seconds"]),
            min_messages=int(row["min_messages"]),
        )

    @staticmethod
    def _draw_entry_from_row(row: aiosqlite.Row) -> DrawEntry:
        return DrawEntry(
            round_id=int(row["round_id"]),
            giveaway_id=int(row["giveaway_id"]),
            telegram_user_id=int(row["telegram_user_id"]),
            telegram_name=str(row["telegram_name"]),
            telegram_username=(
                str(row["telegram_username"])
                if row["telegram_username"] is not None
                else None
            ),
            twitch_login=str(row["twitch_login"]),
            twitch_user_id=str(row["twitch_user_id"]),
            registered_at=int(row["registered_at"]),
            seconds=int(row["seconds"]),
            messages=int(row["messages"]),
            time_requirement_met=bool(row["time_requirement_met"]),
            message_requirement_met=bool(row["message_requirement_met"]),
            excluded_by_twitch=bool(row["excluded_by_twitch"]),
            excluded_by_telegram=bool(row["excluded_by_telegram"]),
            telegram_member=bool(row["telegram_member"]),
            previous_winner=bool(row["previous_winner"]),
            final_eligible=bool(row["final_eligible"]),
            eligibility_reason=(
                str(row["eligibility_reason"])
                if row["eligibility_reason"] is not None
                else None
            ),
            random_score=str(int(row["random_score"])),
            draw_rank=int(row["draw_rank"]) if row["draw_rank"] is not None else None,
            winner_position=(
                int(row["winner_position"])
                if row["winner_position"] is not None
                else None
            ),
        )

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
