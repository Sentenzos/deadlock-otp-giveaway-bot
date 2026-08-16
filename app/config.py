from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    telegram_required_chat_id: int
    telegram_channel_url: str
    owner_telegram_id: int
    twitch_channel: str
    twitch_bot_login: str
    twitch_oauth_token: str
    twitch_excluded_logins: tuple[str, ...]
    telegram_excluded_usernames: tuple[str, ...]
    twitch_live_check_interval_seconds: int
    giveaway_xlsx_enabled: bool
    database_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        def is_missing(value: str) -> bool:
            return not value or value.startswith("replace_") or value.startswith("123456:")

        def required(name: str) -> str:
            value = os.getenv(name, "").strip()
            if is_missing(value):
                raise RuntimeError(f"Заполните {name} в файле .env.")
            return value

        def required_any(name: str, legacy_name: str) -> str:
            value = os.getenv(name, "").strip()
            if not is_missing(value):
                return value
            legacy_value = os.getenv(legacy_name, "").strip()
            if not is_missing(legacy_value):
                return legacy_value
            raise RuntimeError(f"Заполните {name} в файле .env.")

        def optional_bool(name: str, default: bool) -> bool:
            value = os.getenv(name, "").strip().casefold()
            if not value:
                return default
            if value in {"1", "true", "yes", "on"}:
                return True
            if value in {"0", "false", "no", "off"}:
                return False
            raise RuntimeError(
                f"{name} должен иметь значение true/false, yes/no, on/off или 1/0."
            )

        database_path = Path(required("DATABASE_PATH"))
        telegram_channel_url = required("TELEGRAM_CHANNEL_URL").rstrip("/")
        parsed_channel_url = urlsplit(telegram_channel_url)
        if (
            parsed_channel_url.scheme != "https"
            or parsed_channel_url.hostname not in {"t.me", "www.t.me"}
            or not parsed_channel_url.path.strip("/")
            or parsed_channel_url.query
            or parsed_channel_url.fragment
        ):
            raise RuntimeError(
                "TELEGRAM_CHANNEL_URL должен быть ссылкой вида "
                "https://t.me/channel_name или https://t.me/+invite_code."
            )
        twitch_channel = required("TWITCH_CHANNEL").lower().lstrip("#")
        twitch_bot_login = required("TWITCH_BOT_LOGIN").lower()
        excluded_logins = {
            login.strip().lower().lstrip("@")
            for login in os.getenv("TWITCH_EXCLUDED_LOGINS", "").split(",")
            if login.strip()
        }
        excluded_telegram_usernames = {
            username.strip().casefold().lstrip("@")
            for username in os.getenv("TELEGRAM_EXCLUDED_USERNAMES", "").split(",")
            if username.strip()
        }
        return cls(
            telegram_bot_token=required("TELEGRAM_BOT_TOKEN"),
            telegram_required_chat_id=int(
                required_any("TELEGRAM_REQUIRED_CHAT_ID", "TELEGRAM_GROUP_ID")
            ),
            telegram_channel_url=telegram_channel_url,
            owner_telegram_id=int(required("OWNER_TELEGRAM_ID")),
            twitch_channel=twitch_channel,
            twitch_bot_login=twitch_bot_login,
            twitch_oauth_token=required("TWITCH_OAUTH_TOKEN").removeprefix("oauth:"),
            twitch_excluded_logins=tuple(sorted(excluded_logins)),
            telegram_excluded_usernames=tuple(sorted(excluded_telegram_usernames)),
            twitch_live_check_interval_seconds=int(
                os.getenv("TWITCH_LIVE_CHECK_INTERVAL_SECONDS", "10").strip() or "10"
            ),
            giveaway_xlsx_enabled=optional_bool("GIVEAWAY_XLSX_ENABLED", False),
            database_path=database_path,
        )
