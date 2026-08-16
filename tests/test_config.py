from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from app.config import Settings


class SettingsTests(unittest.TestCase):
    def test_reads_required_chat_id(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "999999:token",
            "OWNER_TELEGRAM_ID": "111",
            "TELEGRAM_REQUIRED_CHAT_ID": "-100222",
            "TELEGRAM_CHANNEL_URL": "https://t.me/deadlock_otp/",
            "TWITCH_CHANNEL": "deadlock_otp",
            "TWITCH_BOT_LOGIN": "deadlock_otp",
            "TWITCH_OAUTH_TOKEN": "oauth:abc",
            "TWITCH_EXCLUDED_LOGINS": "nightbot, StreamElements",
            "TELEGRAM_EXCLUDED_USERNAMES": "@Bad_User, ignored_tg",
            "TWITCH_LIVE_CHECK_INTERVAL_SECONDS": "45",
            "GIVEAWAY_XLSX_ENABLED": "true",
            "DATABASE_PATH": "data/test.sqlite3",
        }
        with patch("app.config.load_dotenv"), patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.telegram_required_chat_id, -100222)
        self.assertEqual(settings.telegram_channel_url, "https://t.me/deadlock_otp")
        self.assertEqual(settings.database_path, Path("data/test.sqlite3"))
        self.assertEqual(settings.twitch_oauth_token, "abc")
        self.assertEqual(
            settings.twitch_excluded_logins,
            ("nightbot", "streamelements"),
        )
        self.assertEqual(
            settings.telegram_excluded_usernames,
            ("bad_user", "ignored_tg"),
        )
        self.assertEqual(settings.twitch_live_check_interval_seconds, 45)
        self.assertTrue(settings.giveaway_xlsx_enabled)

    def test_supports_legacy_group_id_name(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "999999:token",
            "OWNER_TELEGRAM_ID": "111",
            "TELEGRAM_GROUP_ID": "-100333",
            "TELEGRAM_CHANNEL_URL": "https://t.me/+invite_code",
            "TWITCH_CHANNEL": "deadlock_otp",
            "TWITCH_BOT_LOGIN": "deadlock_otp",
            "TWITCH_OAUTH_TOKEN": "abc",
            "DATABASE_PATH": "data/test.sqlite3",
        }
        with patch("app.config.load_dotenv"), patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.telegram_required_chat_id, -100333)
        self.assertEqual(settings.twitch_live_check_interval_seconds, 10)
        self.assertFalse(settings.giveaway_xlsx_enabled)

    def test_rejects_invalid_channel_url(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "999999:token",
            "OWNER_TELEGRAM_ID": "111",
            "TELEGRAM_REQUIRED_CHAT_ID": "-100222",
            "TELEGRAM_CHANNEL_URL": "https://example.com/not-telegram",
            "TWITCH_CHANNEL": "deadlock_otp",
            "TWITCH_BOT_LOGIN": "deadlock_otp",
            "TWITCH_OAUTH_TOKEN": "abc",
            "DATABASE_PATH": "data/test.sqlite3",
        }
        with patch("app.config.load_dotenv"), patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "TELEGRAM_CHANNEL_URL"):
                Settings.from_env()

    def test_rejects_invalid_xlsx_flag(self) -> None:
        env = {
            "TELEGRAM_BOT_TOKEN": "999999:token",
            "OWNER_TELEGRAM_ID": "111",
            "TELEGRAM_REQUIRED_CHAT_ID": "-100222",
            "TELEGRAM_CHANNEL_URL": "https://t.me/deadlock_otp",
            "TWITCH_CHANNEL": "deadlock_otp",
            "TWITCH_BOT_LOGIN": "deadlock_otp",
            "TWITCH_OAUTH_TOKEN": "abc",
            "GIVEAWAY_XLSX_ENABLED": "maybe",
            "DATABASE_PATH": "data/test.sqlite3",
        }
        with patch("app.config.load_dotenv"), patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GIVEAWAY_XLSX_ENABLED"):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
