from __future__ import annotations

import unittest

from app.main import (
    giveaway_finish_announcement,
    giveaway_start_announcement,
    giveaway_datetime_text,
    parse_giveaway_end_at,
    parse_start_args,
    personal_stats_text,
    public_bot_commands,
    public_tracking_text,
)
from app.storage import Candidate, Giveaway
from app.twitch_chat import TwitchChatState


class MainCommandTests(unittest.TestCase):
    def test_parse_start_args_with_prize(self) -> None:
        parsed = parse_start_args(
            "start 60 5 3 30 10 Розыгрыш ключей | Steam key".split()
        )

        self.assertEqual(parsed, (60, 5, 3, 30, 10, None, "Розыгрыш ключей", "Steam key"))

    def test_parse_start_args_keeps_old_format_compatible(self) -> None:
        parsed = parse_start_args("start 60 5 3 30 Розыгрыш ключей".split())

        self.assertEqual(parsed, (60, 5, 3, 30, 1, None, "Розыгрыш ключей", ""))

    def test_parse_start_args_with_planned_end(self) -> None:
        parsed = parse_start_args(
            "start 60 5 3 30 10 --end 31.12.2099 23:59 Розыгрыш ключей | Steam key".split()
        )

        self.assertEqual(parsed[5], parse_giveaway_end_at("31.12.2099", "23:59"))
        self.assertEqual(parsed[6:], ("Розыгрыш ключей", "Steam key"))
        self.assertEqual(giveaway_datetime_text(parsed[5]), "31.12.2099 в 23:59 (МСК)")

    def test_parse_start_args_rejects_invalid_end(self) -> None:
        with self.assertRaisesRegex(ValueError, "ДД\\.ММ\\.ГГГГ"):
            parse_start_args(
                "start 60 5 1 0 1 --end 32.13.2099 25:61 Тест".split()
            )

    def test_public_bot_commands_do_not_include_admin_aliases(self) -> None:
        commands = {command.command for command in public_bot_commands()}

        self.assertEqual(commands, {"start", "link", "status"})
        self.assertFalse(any(command.startswith("giveaway") for command in commands))

    def test_announcement_contains_bot_deep_link(self) -> None:
        end_at = parse_giveaway_end_at("31.12.2099", "23:59")
        giveaway = Giveaway(
            1, "active", "Тест", "Приз", 1, 10, 3600, 5, 30, 1, None, None, end_at
        )

        text = giveaway_start_announcement(giveaway, "deadlock_otp_bot")

        self.assertIn("https://t.me/deadlock_otp_bot?start=link", text)
        self.assertIn("регистрация нужна заново", text)
        self.assertIn("🎁 <b>Награда</b>", text)
        self.assertIn("📋 <b>Условия участия</b>", text)
        self.assertIn("✅ <b>Как принять участие</b>", text)
        self.assertIn("🗓 <b>Плановое завершение</b>", text)
        self.assertIn("31.12.2099 в 23:59 (МСК)", text)
        self.assertIn("не менее <b>10</b> допущенных участников", text)
        self.assertIn("Время просмотра трансляции:", text)
        self.assertNotIn("Время в Twitch-чате:", text)

    def test_personal_stats_text_shows_progress(self) -> None:
        giveaway = Giveaway(1, "active", "Тест", "", 1, 1, 3600, 5, 0, 1, None, None)
        stats = Candidate(101, "Alice", "alice_tv", 3600, 4)

        text = personal_stats_text(giveaway, stats)

        self.assertIn("alice_tv", text)
        self.assertIn("Время: 1 ч 0 мин из 60 мин ✅", text)
        self.assertIn("Сообщения: 4 из 5 ⏳", text)

    def test_public_status_explains_minimum_for_winner_selection(self) -> None:
        giveaway = Giveaway(1, "active", "Тест", "", 1, 10, 3600, 5, 0, 1, None, None)

        text = public_tracking_text(giveaway, TwitchChatState(), participant_count=7)

        self.assertIn(
            "Победители будут выбраны, если к завершению будет не менее 10 допущенных участников.",
            text,
        )
        self.assertIn("Зарегистрировано участников: <b>7</b>", text)

    def test_finish_announcement_is_created_without_winners(self) -> None:
        giveaway = Giveaway(1, "finished", "Тест", "Приз", 1, 2, 60, 1, 0, 1, 2, 0)

        header, lines = giveaway_finish_announcement(giveaway, [])

        self.assertIn("Розыгрыш завершён", header)
        self.assertIn("🎁 <b>Награда</b>", header)
        self.assertIn("Приз", header)
        self.assertIn("ℹ️ <b>Итог</b>", lines)
        self.assertIn("Победители не выбраны.", lines)
        self.assertTrue(any("Допущенных участников: <b>0</b>" in line for line in lines))

    def test_finish_announcement_contains_telegram_and_twitch_winner(self) -> None:
        giveaway = Giveaway(1, "finished", "Тест", "", 1, 1, 60, 1, 0, 1, 2, 1)
        winner = Candidate(101, "Alice", "alice_tv", 60, 1, "alice_tg")

        _, lines = giveaway_finish_announcement(giveaway, [winner])

        combined = "\n".join(lines)
        self.assertIn("🏆 <b>Победители</b>", combined)
        self.assertIn('tg://user?id=101', combined)
        self.assertIn("@alice_tg", combined)
        self.assertIn("alice_tv", combined)


if __name__ == "__main__":
    unittest.main()
