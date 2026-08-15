from __future__ import annotations

import asyncio
from contextlib import suppress
import html
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import BotCommand, BotCommandScopeChat, BufferedInputFile, Message

from .config import Settings
from .giveaway_report import (
    RESULT_NOT_ELIGIBLE,
    RESULT_NOT_SELECTED,
    RESULT_WINNER,
    GiveawayReportMetadata,
    GiveawayReportParticipant,
    GiveawayReportRound,
    build_giveaway_report,
)
from .storage import (
    Candidate,
    DrawEligibility,
    DrawEntry,
    DrawParticipant,
    DrawResult,
    Giveaway,
    Storage,
)
from .twitch_announce import TwitchGiveawayAnnouncer
from .twitch_chat import TwitchChat, TwitchChatState, TwitchLiveMonitor

logger = logging.getLogger(__name__)
MOSCOW_TIMEZONE = timezone(timedelta(hours=3), name="МСК")
STREAM_ANNOUNCE_COOLDOWN_SECONDS = 30 * 60
PUBLIC_COMMAND_COOLDOWNS_SECONDS = {
    "start": 3,
    "status": 5,
    "link": 10,
}
LAST_STREAM_OFFLINE_STATE_KEY = "last_stream_offline_at"
LAST_ANNOUNCED_STREAM_STATE_KEY = "last_announced_stream_started_at"
GIVEAWAY_EDIT_FIELD_ALIASES = {
    "minutes": "min_minutes",
    "min_minutes": "min_minutes",
    "минуты": "min_minutes",
    "время": "min_minutes",
    "messages": "min_messages",
    "min_messages": "min_messages",
    "сообщения": "min_messages",
    "winners": "winner_count",
    "winner_count": "winner_count",
    "победители": "winner_count",
    "interval": "message_interval_seconds",
    "message_interval": "message_interval_seconds",
    "интервал": "message_interval_seconds",
    "participants": "min_participants",
    "min_participants": "min_participants",
    "участники": "min_participants",
    "end": "end_at",
    "date": "end_at",
    "завершение": "end_at",
    "title": "title",
    "name": "title",
    "название": "title",
    "prize": "prize",
    "reward": "prize",
    "награда": "prize",
}
GIVEAWAY_EDIT_CLEAR_VALUES = {
    "clear",
    "none",
    "remove",
    "очистить",
    "убрать",
    "нет",
    "-",
}
ConfigureTwitchAnnouncements = Callable[
    [bool, int | None], Awaitable[Giveaway | None]
]
ValidateTwitchChatSend = Callable[[], Awaitable[None]]


def duration_text(seconds: int) -> str:
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


def giveaway_datetime_text(timestamp: int) -> str:
    value = datetime.fromtimestamp(timestamp, MOSCOW_TIMEZONE)
    return value.strftime("%d.%m.%Y в %H:%M (МСК)")


def twitch_profile_link(login: str) -> str:
    clean_login = login.lstrip("@#")
    profile_url = f"https://www.twitch.tv/{quote(clean_login, safe='')}"
    return (
        f'<a href="{html.escape(profile_url, quote=True)}">'
        f"{html.escape(clean_login)}</a>"
    )


def parse_giveaway_end_at(date_text: str, time_text: str) -> int:
    try:
        value = datetime.strptime(
            f"{date_text} {time_text}", "%d.%m.%Y %H:%M"
        ).replace(tzinfo=MOSCOW_TIMEZONE)
    except ValueError as error:
        raise ValueError(
            "Дата завершения должна быть в формате ДД.ММ.ГГГГ ЧЧ:ММ."
        ) from error
    timestamp = int(value.timestamp())
    if timestamp <= int(time.time()):
        raise ValueError("Дата и время завершения должны быть в будущем.")
    return timestamp


def ago_text(timestamp: int | None) -> str:
    if timestamp is None:
        return "нет данных"
    seconds = max(0, int(time.time()) - timestamp)
    if seconds < 60:
        return "меньше минуты назад"
    return f"{duration_text(seconds)} назад"


def twitch_announcement_status_text(
    giveaway: Giveaway, twitch_state: TwitchChatState
) -> str:
    if not giveaway.twitch_announce_enabled:
        return "Twitch-анонсы розыгрыша: <b>выключены</b>"
    interval_minutes = giveaway.twitch_announce_interval_seconds // 60
    if giveaway.twitch_last_announce_at is None:
        schedule = "Первый анонс: при ближайшей возможности"
    else:
        next_at = (
            giveaway.twitch_last_announce_at
            + giveaway.twitch_announce_interval_seconds
        )
        schedule = (
            f"Последний анонс: {ago_text(giveaway.twitch_last_announce_at)}\n"
            + (
                "Следующий анонс: при ближайшей проверке"
                if next_at <= int(time.time())
                else f"Следующий анонс: {giveaway_datetime_text(next_at)}"
            )
        )
    if twitch_state.last_stream_error is not None:
        pause = "\nСостояние: на паузе, не удалось подтвердить актуальный live-статус"
    elif not twitch_state.stream_live:
        pause = "\nСостояние: на паузе, стрим offline"
    elif not twitch_state.connected:
        pause = "\nСостояние: на паузе, Twitch-чат не подключён"
    else:
        pause = "\nСостояние: активно"
    return (
        "Twitch-анонсы розыгрыша: <b>включены</b>\n"
        f"Периодичность: каждые <b>{interval_minutes} мин</b>\n"
        f"{schedule}{pause}"
    )


def twitch_diagnostics_text(settings: Settings, twitch_state: TwitchChatState) -> str:
    connection = "подключён" if twitch_state.connected else "не подключён"
    live = "live" if twitch_state.stream_live else "offline"
    if not twitch_state.stream_known:
        live = "пока неизвестно"
    return (
        f"Twitch-сборщик: <b>{connection}</b>\n"
        f"Стрим: <b>{live}</b>\n"
        f"Последняя проверка live: {ago_text(twitch_state.last_stream_check_at)}\n"
        f"Название стрима: {html.escape(twitch_state.stream_title or 'нет')}\n"
        f"Канал: <code>{html.escape(settings.twitch_channel)}</code>\n"
        f"Последний сигнал от Twitch: {ago_text(twitch_state.last_irc_at)}\n"
        f"Последнее сообщение в Twitch-чате: {ago_text(twitch_state.last_privmsg_at)}\n"
        f"Сообщений увидено с запуска: {twitch_state.messages_seen}\n"
        f"Попыток !link с запуска: {twitch_state.link_attempts}\n"
        f"Успешных привязок с запуска: {twitch_state.successful_links}\n"
        f"Последний исходящий Twitch-анонс: {ago_text(twitch_state.last_chat_send_at)}\n"
        f"Ошибка отправки Twitch-анонса: {html.escape(twitch_state.last_chat_send_error or 'нет')}\n"
        f"Последний Twitch NOTICE: {html.escape(twitch_state.last_notice or 'нет')}\n"
        f"Последняя ошибка Twitch-чата: {html.escape(twitch_state.last_error or 'нет')}\n"
        f"Последняя ошибка live-проверки: {html.escape(twitch_state.last_stream_error or 'нет')}"
    )


def public_tracking_text(
    giveaway: Giveaway | None,
    twitch_state: TwitchChatState,
    participant_count: int | None = None,
) -> str:
    if giveaway is None:
        return "Учёт минут и сообщений сейчас не активен: нет открытого розыгрыша."
    if not twitch_state.stream_known:
        state = "Учёт пока не начат: бот ещё проверяет, идёт ли стрим."
    elif not twitch_state.stream_live:
        state = "Учёт сейчас на паузе: стрим не live."
    elif not twitch_state.connected:
        state = "Учёт сейчас под вопросом: стрим live, но бот переподключается к Twitch-чату."
    else:
        state = "Учёт активен: стрим live, бот видит Twitch-чат."
    return (
        f"{state}\n\n"
        f"Розыгрыш: <b>{html.escape(giveaway.title)}</b>\n"
        + (f"Награда: <b>{html.escape(giveaway.prize)}</b>\n" if giveaway.prize else "")
        + (
            f"Плановое завершение: <b>{giveaway_datetime_text(giveaway.end_at)}</b>\n"
            if giveaway.end_at is not None
            else ""
        )
        + (
        f"Условия: {giveaway.min_seconds // 60} мин в Twitch-чате и "
        f"{giveaway.min_messages} сообщений\n"
        f"Интервал сообщений: {giveaway.message_interval_seconds} сек\n"
        f"Победителей: {giveaway.winner_count}\n"
        + (
            f"Зарегистрировано участников: <b>{participant_count}</b>\n"
            if participant_count is not None
            else ""
        )
        + (
        "Победители будут выбраны, если к завершению будет не менее "
        f"{giveaway.min_participants} допущенных участников."
        )
        )
    )


async def is_still_in_required_chat(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except TelegramAPIError:
        logger.warning("Не удалось проверить участника Telegram %s", user_id, exc_info=True)
        return False
    if member.status in {"creator", "owner", "administrator", "member"}:
        return True
    return member.status == "restricted" and bool(getattr(member, "is_member", False))


async def group_candidates(
    bot: Bot, settings: Settings, storage: Storage, giveaway: Giveaway
) -> list[Candidate]:
    candidates = await storage.eligible_candidates(
        giveaway,
        settings.twitch_excluded_logins,
        settings.telegram_excluded_usernames,
    )
    return [
        candidate
        for candidate in candidates
        if await is_still_in_required_chat(
            bot, settings.telegram_required_chat_id, candidate.telegram_user_id
        )
    ]


class TelegramMembershipCheckError(RuntimeError):
    """The draw must remain unchanged until Telegram membership can be verified."""


async def draw_membership_decisions(
    bot: Bot,
    settings: Settings,
    participants: list[DrawParticipant],
) -> dict[int, DrawEligibility]:
    decisions: dict[int, DrawEligibility] = {}
    for participant in participants:
        try:
            member = await bot.get_chat_member(
                settings.telegram_required_chat_id,
                participant.telegram_user_id,
            )
        except TelegramAPIError as error:
            logger.warning(
                "Не удалось проверить участника Telegram %s перед жеребьёвкой",
                participant.telegram_user_id,
                exc_info=True,
            )
            raise TelegramMembershipCheckError(
                "Telegram временно не позволил проверить членство участников."
            ) from error
        is_member = member.status in {
            "creator",
            "owner",
            "administrator",
            "member",
        } or (
            member.status == "restricted"
            and bool(getattr(member, "is_member", False))
        )
        decisions[participant.telegram_user_id] = DrawEligibility(
            telegram_member=is_member,
            reason=None if is_member else "telegram_not_member",
        )
    return decisions


async def create_persisted_draw(
    bot: Bot,
    settings: Settings,
    storage: Storage,
    giveaway: Giveaway,
    *,
    reroll: bool = False,
    force: bool = False,
) -> DrawResult:
    participants = await storage.draw_participants(
        giveaway,
        settings.twitch_excluded_logins,
        settings.telegram_excluded_usernames,
    )
    decisions = await draw_membership_decisions(bot, settings, participants)
    return await storage.create_draw_round(
        giveaway,
        decisions,
        reroll=reroll,
        force=force,
        excluded_twitch_logins=settings.twitch_excluded_logins,
        excluded_telegram_usernames=settings.telegram_excluded_usernames,
        finish=not reroll,
    )


def draw_entry_candidate(entry: DrawEntry) -> Candidate:
    return Candidate(
        telegram_user_id=entry.telegram_user_id,
        telegram_name=entry.telegram_name,
        telegram_username=entry.telegram_username,
        twitch_login=entry.twitch_login,
        seconds=entry.seconds,
        messages=entry.messages,
    )


def giveaway_report_participant(entry: DrawEntry) -> GiveawayReportParticipant:
    if entry.winner_position is not None:
        result = RESULT_WINNER
    elif entry.final_eligible:
        result = RESULT_NOT_SELECTED
    else:
        result = RESULT_NOT_ELIGIBLE
    return GiveawayReportParticipant(
        twitch_login=entry.twitch_login,
        minutes=entry.seconds / 60,
        messages=entry.messages,
        admitted=entry.final_eligible,
        random_number=entry.random_score,
        eligible_place=entry.draw_rank,
        result=result,
    )


async def persisted_giveaway_report(
    storage: Storage, giveaway: Giveaway
) -> tuple[bytes, int] | None:
    rounds = await storage.draw_rounds(giveaway)
    if not rounds:
        return None
    report_rounds: list[GiveawayReportRound] = []
    for draw_round in rounds:
        entries = await storage.draw_entries(draw_round.id)
        report_rounds.append(
            GiveawayReportRound(
                draw_round=draw_round.round_number,
                forced=draw_round.forced,
                participants=tuple(
                    giveaway_report_participant(entry) for entry in entries
                ),
            )
        )
    initial_round = rounds[0]
    metadata = GiveawayReportMetadata(
        giveaway_id=giveaway.id,
        title=initial_round.title,
        prize=initial_round.prize,
        min_minutes=initial_round.min_seconds / 60,
        min_messages=initial_round.min_messages,
        winner_count=initial_round.requested_winner_count,
        min_participants=initial_round.min_participants_required,
        message_interval_seconds=giveaway.message_interval_seconds,
        started_at=giveaway.started_at,
        end_at=giveaway.end_at,
        finished_at=giveaway.finished_at or initial_round.cutoff_at,
        eligible_count=(
            giveaway.eligible_count_at_finish
            if giveaway.eligible_count_at_finish is not None
            else initial_round.eligible_count
        ),
    )
    report_bytes = await asyncio.to_thread(
        build_giveaway_report,
        metadata,
        tuple(report_rounds),
    )
    return report_bytes, rounds[-1].round_number


async def send_persisted_giveaway_report(
    bot: Bot,
    storage: Storage,
    giveaway: Giveaway,
    chat_id: int,
) -> bool | None:
    try:
        prepared = await persisted_giveaway_report(storage, giveaway)
        if prepared is None:
            return None
        report_bytes, latest_round = prepared
        if len(report_bytes) > 49 * 1024 * 1024:
            raise RuntimeError("XLSX-отчёт превышает безопасный размер Telegram-документа.")
        await bot.send_document(
            chat_id=chat_id,
            document=BufferedInputFile(
                report_bytes,
                filename=f"giveaway_{giveaway.id}_results.xlsx",
            ),
            caption=(
                "📊 <b>Публичный протокол жеребьёвки</b>\n"
                f"Розыгрыш: <b>{html.escape(giveaway.title)}</b>\n"
                f"Сохранено раундов: <b>{latest_round}</b>. "
                "Telegram-ники и Telegram ID в файле отсутствуют."
            ),
            parse_mode=ParseMode.HTML,
        )
        return True
    except Exception:
        logger.warning(
            "Не удалось сформировать или отправить XLSX розыгрыша %s в Telegram %s",
            giveaway.id,
            chat_id,
            exc_info=True,
        )
        return False


def is_owner_command(message: Message, settings: Settings) -> bool:
    return (
        message.from_user is not None
        and message.from_user.id == settings.owner_telegram_id
        and (
            message.chat.type == ChatType.PRIVATE
            or message.chat.id == settings.telegram_required_chat_id
        )
    )


def pick_winners(candidates: list[Candidate], winner_count: int) -> list[Candidate]:
    pool = list(candidates)
    winners: list[Candidate] = []
    for _ in range(min(winner_count, len(pool))):
        winners.append(pool.pop(secrets.randbelow(len(pool))))
    return winners


def format_winner_lines(winners: list[Candidate]) -> list[str]:
    lines = []
    for index, winner in enumerate(winners, start=1):
        telegram_label = (
            f"@{html.escape(winner.telegram_username)}"
            if winner.telegram_username
            else html.escape(winner.telegram_name)
        )
        lines.append(
            f"{index}. Telegram: <a href=\"tg://user?id={winner.telegram_user_id}\">"
            f"{telegram_label}</a> "
            f"- Twitch: <code>{html.escape(winner.twitch_login)}</code>, "
            f"{duration_text(winner.seconds)} в чате, {winner.messages} сообщений"
        )
    return lines


def winner_private_message(giveaway: Giveaway, winner: Candidate) -> str:
    prize_line = (
        f"\nНаграда: <b>{html.escape(giveaway.prize)}</b>"
        if giveaway.prize
        else ""
    )
    return (
        "🏆 <b>Поздравляем! Вы победили в розыгрыше!</b>\n\n"
        f"Розыгрыш: <b>{html.escape(giveaway.title)}</b>"
        f"{prize_line}\n"
        f"Ваш Twitch-аккаунт: <code>{html.escape(winner.twitch_login)}</code>\n\n"
        "Организатор свяжется с вами по поводу получения награды."
    )


async def notify_winners(
    bot: Bot, giveaway: Giveaway, winners: list[Candidate]
) -> list[Candidate]:
    failed: list[Candidate] = []
    for winner in winners:
        try:
            await bot.send_message(
                winner.telegram_user_id,
                winner_private_message(giveaway, winner),
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            failed.append(winner)
            logger.warning(
                "Не удалось уведомить победителя Telegram %s о розыгрыше %s",
                winner.telegram_user_id,
                giveaway.id,
                exc_info=True,
            )
    return failed


def split_title_and_prize(text: str) -> tuple[str, str]:
    title, separator, prize = text.partition("|")
    if not separator:
        return text.strip() or "Розыгрыш", ""
    return title.strip() or "Розыгрыш", prize.strip()


def parse_start_args(
    parts: list[str],
) -> tuple[int, int, int, int, int, int | None, str, str]:
    if len(parts) < 3:
        raise ValueError("Укажите минуты и количество сообщений.")
    try:
        min_minutes = int(parts[1])
        min_messages = int(parts[2])
    except ValueError as error:
        raise ValueError("Минуты и сообщения должны быть целыми числами.") from error
    winner_count = 1
    message_interval_seconds = 0
    min_participants = 1
    end_at: int | None = None
    index = 3
    if index < len(parts) and parts[index].isdigit():
        winner_count = int(parts[index])
        index += 1
    if index < len(parts) and parts[index].isdigit():
        message_interval_seconds = int(parts[index])
        index += 1
    if index < len(parts) and parts[index].isdigit():
        min_participants = int(parts[index])
        index += 1
    if index < len(parts) and parts[index].casefold() == "--end":
        if index + 2 >= len(parts):
            raise ValueError(
                "После --end укажите дату и время: ДД.ММ.ГГГГ ЧЧ:ММ."
            )
        end_at = parse_giveaway_end_at(parts[index + 1], parts[index + 2])
        index += 3
    title, prize = split_title_and_prize(" ".join(parts[index:]))
    return (
        min_minutes,
        min_messages,
        winner_count,
        message_interval_seconds,
        min_participants,
        end_at,
        title,
        prize,
    )


def parse_edit_args(raw_args: str) -> tuple[str, int | str | None]:
    parts = raw_args.split(maxsplit=2)
    if len(parts) < 2:
        raise ValueError("Укажите параметр, который нужно изменить.")
    requested_field = parts[1].casefold()
    field = GIVEAWAY_EDIT_FIELD_ALIASES.get(requested_field)
    if field is None:
        raise ValueError(
            f"Неизвестный параметр «{parts[1]}». Доступны: minutes, messages, "
            "winners, interval, participants, end, title и prize."
        )
    if len(parts) < 3 or not parts[2].strip():
        if field == "end_at":
            raise ValueError(
                "Укажите дату и время завершения либо clear для удаления даты."
            )
        if field == "prize":
            raise ValueError("Укажите новую награду либо clear для её удаления.")
        raise ValueError("Укажите новое значение параметра.")

    value_text = parts[2].strip()
    if field in {
        "min_minutes",
        "min_messages",
        "winner_count",
        "message_interval_seconds",
        "min_participants",
    }:
        if len(value_text.split()) != 1:
            raise ValueError("Для числового параметра укажите ровно одно целое число.")
        try:
            return field, int(value_text)
        except ValueError as error:
            raise ValueError("Значение параметра должно быть целым числом.") from error

    if field == "end_at":
        if value_text.casefold() in GIVEAWAY_EDIT_CLEAR_VALUES:
            return field, None
        date_parts = value_text.split()
        if len(date_parts) != 2:
            raise ValueError(
                "Дата завершения указывается как ДД.ММ.ГГГГ ЧЧ:ММ либо clear."
            )
        return field, parse_giveaway_end_at(date_parts[0], date_parts[1])

    if field == "prize" and value_text.casefold() in GIVEAWAY_EDIT_CLEAR_VALUES:
        return field, ""
    return field, value_text


def giveaway_edit_usage() -> str:
    return (
        "Использование:\n"
        "<code>/giveaway edit minutes 100</code>\n"
        "<code>/giveaway edit messages 10</code>\n"
        "<code>/giveaway edit winners 1</code>\n"
        "<code>/giveaway edit interval 600</code>\n"
        "<code>/giveaway edit participants 10</code>\n"
        "<code>/giveaway edit end ДД.ММ.ГГГГ ЧЧ:ММ</code> или "
        "<code>/giveaway edit end clear</code>\n"
        "<code>/giveaway edit title Новое название</code>\n"
        "<code>/giveaway edit prize Новая награда</code> или "
        "<code>/giveaway edit prize clear</code>"
    )


def giveaway_start_announcement(giveaway: Giveaway, bot_username: str) -> str:
    clean_bot_username = bot_username.lstrip("@")
    bot_link = f"https://t.me/{clean_bot_username}?start=link"
    lines = [
        "🎉 <b>Розыгрыш начался</b>",
        f"<b>{html.escape(giveaway.title)}</b>",
    ]
    if giveaway.prize:
        lines.extend(["", "🎁 <b>Награда</b>", html.escape(giveaway.prize)])
    if giveaway.end_at is not None:
        lines.extend(
            [
                "",
                "🗓 <b>Плановое завершение</b>",
                f"<b>{giveaway_datetime_text(giveaway.end_at)}</b>",
            ]
        )
    lines.extend(
        [
            "",
            "📋 <b>Условия участия</b>",
            f"• Время просмотра трансляции: не менее <b>{giveaway.min_seconds // 60} мин</b>",
            f"• Сообщения в Twitch-чате: не менее <b>{giveaway.min_messages}</b>",
            f"• Интервал между засчитанными сообщениями: <b>{giveaway.message_interval_seconds} сек</b>",
            f"• Количество победителей: <b>{giveaway.winner_count}</b>",
            (
                "• Для выбора победителей нужно не менее "
                f"<b>{giveaway.min_participants}</b> допущенных участников"
            ),
            "",
            "🟣 <i>Время и сообщения считаются только во время live-стрима.</i>",
            "",
            "✅ <b>Как принять участие</b>",
            (
                f"1. <a href=\"{html.escape(bot_link, quote=True)}\">Откройте личный чат с ботом</a>."
            ),
            "2. Отправьте команду <code>/link</code> и следуйте подсказкам бота.",
            "",
            "⚠️ <i>Для каждого нового розыгрыша регистрация нужна заново.</i>",
        ]
    )
    return "\n".join(lines)


def giveaway_finish_announcement(
    giveaway: Giveaway, winners: list[Candidate]
) -> tuple[str, list[str]]:
    header = f"🏁 <b>Розыгрыш завершён</b>\n<b>{html.escape(giveaway.title)}</b>"
    if giveaway.prize:
        header += f"\n\n🎁 <b>Награда</b>\n{html.escape(giveaway.prize)}"
    if giveaway.finished_at is not None:
        header += (
            f"\n\n🗓 <b>Завершено</b>\n"
            f"{giveaway_datetime_text(giveaway.finished_at)}"
        )
    if winners:
        lines = ["", "🏆 <b>Победители</b>", *format_winner_lines(winners)]
        if (
            giveaway.eligible_count_at_finish is not None
            and giveaway.eligible_count_at_finish < giveaway.min_participants
        ):
            lines.extend(
                [
                    "",
                    "⚠️ <i>Установленный минимум участников не был достигнут. "
                    "Победители выбраны владельцем в принудительном режиме.</i>",
                ]
            )
        lines.extend(["", f"📊 <b>Итог:</b> выбрано победителей — <b>{len(winners)}</b>."])
        return header, lines

    lines = ["", "ℹ️ <b>Итог</b>", "Победители не выбраны."]
    if (
        giveaway.eligible_count_at_finish is not None
        and giveaway.eligible_count_at_finish < giveaway.min_participants
    ):
        lines.append(
            f"• Допущенных участников: <b>{giveaway.eligible_count_at_finish}</b>\n"
            f"• Для выбора требовалось: не менее <b>{giveaway.min_participants}</b>"
        )
    else:
        lines.append("У этого розыгрыша нет записанных победителей.")
    return header, lines


def personal_stats_text(giveaway: Giveaway, stats: Candidate | None) -> str:
    if stats is None:
        return (
            "<b>Ваша статистика:</b>\n"
            "Вы ещё не зарегистрированы в этом розыгрыше. Выполните /link, "
            "даже если привязывали Twitch раньше."
        )
    time_ready = stats.seconds >= giveaway.min_seconds
    messages_ready = stats.messages >= giveaway.min_messages
    ready = time_ready and messages_ready
    return (
        "<b>Ваша статистика:</b>\n"
        f"Twitch: <code>{html.escape(stats.twitch_login)}</code>\n"
        f"Время: {duration_text(stats.seconds)} из {giveaway.min_seconds // 60} мин "
        f"{'✅' if time_ready else '⏳'}\n"
        f"Сообщения: {stats.messages} из {giveaway.min_messages} "
        f"{'✅' if messages_ready else '⏳'}\n"
        + (
            "Технические условия выполнены ✅"
            if ready
            else "Технические условия пока не выполнены."
        )
    )


def giveaway_created_text(giveaway: Giveaway) -> str:
    return (
        f"Розыгрыш создан, но ещё не анонсирован: <b>{html.escape(giveaway.title)}</b>\n"
        + (f"Награда: <b>{html.escape(giveaway.prize)}</b>\n" if giveaway.prize else "")
        + (
            f"Плановое завершение: <b>{giveaway_datetime_text(giveaway.end_at)}</b>\n"
            if giveaway.end_at is not None
            else ""
        )
        + (
        f"Условия: {giveaway.min_seconds // 60} мин, {giveaway.min_messages} сообщений\n"
        f"Интервал сообщений: {giveaway.message_interval_seconds} сек\n"
        f"Победителей: {giveaway.winner_count}\n"
        "Для выбора победителей к завершению необходимо не менее "
        f"{giveaway.min_participants} допущенных участников.\n\n"
        "Чтобы опубликовать старт в канал/чат, используйте "
        "<code>/giveaway announce_start</code>."
        )
    )


def giveaway_parameter_value(
    giveaway: Giveaway, parameter: str
) -> int | str | None:
    values: dict[str, int | str | None] = {
        "min_minutes": giveaway.min_seconds // 60,
        "min_messages": giveaway.min_messages,
        "winner_count": giveaway.winner_count,
        "message_interval_seconds": giveaway.message_interval_seconds,
        "min_participants": giveaway.min_participants,
        "end_at": giveaway.end_at,
        "title": giveaway.title,
        "prize": giveaway.prize,
    }
    return values[parameter]


def giveaway_rules_signature(giveaway: Giveaway) -> tuple[object, ...]:
    return (
        giveaway.title,
        giveaway.prize,
        giveaway.winner_count,
        giveaway.min_participants,
        giveaway.min_seconds,
        giveaway.min_messages,
        giveaway.message_interval_seconds,
        giveaway.end_at,
    )


def giveaway_parameter_display(giveaway: Giveaway, parameter: str) -> str:
    value = giveaway_parameter_value(giveaway, parameter)
    if parameter == "min_minutes":
        return f"{value} мин"
    if parameter == "min_messages":
        return f"{value} сообщений"
    if parameter == "winner_count":
        return f"{value}"
    if parameter == "message_interval_seconds":
        return f"{value} сек"
    if parameter == "min_participants":
        return f"{value}"
    if parameter == "end_at":
        return giveaway_datetime_text(value) if isinstance(value, int) else "не задано"
    if parameter == "prize":
        return html.escape(str(value)) if value else "не указана"
    return html.escape(str(value))


def giveaway_updated_text(
    previous: Giveaway,
    updated: Giveaway,
    parameter: str,
    finish_confirmation_cancelled: bool = False,
) -> str:
    labels = {
        "min_minutes": "Минимальное время",
        "min_messages": "Минимум сообщений",
        "winner_count": "Количество победителей",
        "message_interval_seconds": "Интервал между сообщениями",
        "min_participants": "Минимум допущенных участников",
        "end_at": "Плановое завершение",
        "title": "Название",
        "prize": "Награда",
    }
    changed = giveaway_parameter_value(previous, parameter) != giveaway_parameter_value(
        updated, parameter
    )
    if not changed:
        return (
            "ℹ️ Параметр уже имеет указанное значение.\n\n"
            f"{labels[parameter]}: <b>{giveaway_parameter_display(updated, parameter)}</b>."
        )
    extra = ""
    if parameter == "message_interval_seconds":
        extra = (
            "\nНовый интервал применяется только к будущим сообщениям; ранее "
            "засчитанные сообщения не пересчитываются."
        )
    if finish_confirmation_cancelled:
        extra += (
            "\nПредыдущее подтверждение завершения отменено. Чтобы завершить "
            "розыгрыш, снова выполните <code>/giveaway finish</code>."
        )
    return (
        "✅ <b>Параметр активного розыгрыша обновлён</b>\n"
        f"{labels[parameter]}: "
        f"<b>{giveaway_parameter_display(previous, parameter)}</b> → "
        f"<b>{giveaway_parameter_display(updated, parameter)}</b>.\n\n"
        "Накопленные минуты, сообщения и регистрации участников сохранены."
        f"{extra}\n\n"
        "Уже опубликованный Telegram-анонс не изменится автоматически. "
        "При необходимости отправьте <code>/giveaway announce_start</code> повторно."
    )


def html_chunks(header: str, lines: list[str]) -> Iterator[str]:
    chunk = header
    for line in lines:
        addition = f"\n{line}"
        if len(chunk) + len(addition) > 3900:
            yield chunk
            chunk = f"Продолжение:\n{line}"
        else:
            chunk += addition
    yield chunk


async def answer_long_html(message: Message, header: str, lines: list[str]) -> None:
    for chunk in html_chunks(header, lines):
        await message.answer(chunk, parse_mode=ParseMode.HTML)


async def publish_html(message: Message, bot: Bot, settings: Settings, text: str) -> None:
    if message.chat.id == settings.telegram_required_chat_id:
        await message.answer(text, parse_mode=ParseMode.HTML)
        return
    try:
        await bot.send_message(settings.telegram_required_chat_id, text, parse_mode=ParseMode.HTML)
    except TelegramAPIError:
        logger.warning("Не удалось опубликовать сообщение в Telegram %s", settings.telegram_required_chat_id, exc_info=True)
        await message.answer(
            "Не удалось опубликовать сообщение в Telegram-канал/чат. "
            "Проверьте, что бот добавлен туда администратором.",
        )
        return
    await message.answer("Опубликовал сообщение в Telegram-канал/чат.")


async def publish_long_html(
    message: Message, bot: Bot, settings: Settings, header: str, lines: list[str]
) -> None:
    if message.chat.id == settings.telegram_required_chat_id:
        await answer_long_html(message, header, lines)
        return
    try:
        for chunk in html_chunks(header, lines):
            await bot.send_message(
                settings.telegram_required_chat_id, chunk, parse_mode=ParseMode.HTML
            )
    except TelegramAPIError:
        logger.warning("Не удалось опубликовать сообщение в Telegram %s", settings.telegram_required_chat_id, exc_info=True)
        await message.answer(
            "Не удалось опубликовать сообщение в Telegram-канал/чат. "
            "Проверьте, что бот добавлен туда администратором.",
        )
        return
    await message.answer("Опубликовал сообщение в Telegram-канал/чат.")


def twitch_stream_started_announcement(
    settings: Settings, twitch_state: TwitchChatState
) -> str:
    channel = html.escape(settings.twitch_channel)
    channel_url = f"https://www.twitch.tv/{settings.twitch_channel}"
    title = html.escape(twitch_state.stream_title or f"Стрим {settings.twitch_channel}")
    return (
        "🔴 <b>Стрим начался!</b>\n\n"
        f"🎥 <b>{title}</b>\n"
        f"📺 <a href=\"{html.escape(channel_url, quote=True)}\">Смотреть {channel} на Twitch</a>"
    )


async def handle_stream_state_change(
    is_live: bool,
    bot: Bot,
    settings: Settings,
    storage: Storage,
    twitch_state: TwitchChatState,
) -> None:
    if not is_live:
        await storage.end_live_tracking()
        await storage.set_runtime_state(
            LAST_STREAM_OFFLINE_STATE_KEY, str(int(time.time()))
        )
        return

    await storage.begin_live_tracking(settings.twitch_excluded_logins)
    stream_started_at = twitch_state.stream_started_at or ""
    last_announced_stream = await storage.runtime_state(
        LAST_ANNOUNCED_STREAM_STATE_KEY
    )
    if stream_started_at and stream_started_at == last_announced_stream:
        logger.info("Анонс Twitch-стрима уже был опубликован")
        return

    last_offline_raw = await storage.runtime_state(LAST_STREAM_OFFLINE_STATE_KEY)
    try:
        last_offline_at = int(last_offline_raw) if last_offline_raw is not None else None
    except ValueError:
        last_offline_at = None
    offline_seconds = (
        int(time.time()) - last_offline_at if last_offline_at is not None else None
    )
    if (
        offline_seconds is not None
        and 0 <= offline_seconds < STREAM_ANNOUNCE_COOLDOWN_SECONDS
    ):
        logger.info(
            "Пропускаю анонс Twitch: offline длился %s секунд",
            offline_seconds,
        )
        if stream_started_at:
            await storage.set_runtime_state(
                LAST_ANNOUNCED_STREAM_STATE_KEY, stream_started_at
            )
        return

    try:
        await bot.send_message(
            settings.telegram_required_chat_id,
            twitch_stream_started_announcement(settings, twitch_state),
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        logger.warning("Не удалось опубликовать анонс Twitch-стрима", exc_info=True)
        return
    if stream_started_at:
        await storage.set_runtime_state(
            LAST_ANNOUNCED_STREAM_STATE_KEY, stream_started_at
        )


def public_bot_commands() -> list[BotCommand]:
    return [
        BotCommand(command="start", description="Что умеет бот"),
        BotCommand(command="link", description="Зарегистрироваться в розыгрыше"),
        BotCommand(command="status", description="Статус учёта и моя статистика"),
    ]


def owner_bot_commands() -> list[BotCommand]:
    return [
        *public_bot_commands(),
        BotCommand(command="viewers", description="Участники Twitch-чата сейчас"),
        BotCommand(command="twitch_announce", description="Анонсы розыгрыша в Twitch"),
        BotCommand(command="giveaway_create", description="Создать розыгрыш"),
        BotCommand(command="giveaway_edit", description="Изменить параметры розыгрыша"),
        BotCommand(command="giveaway_announce_start", description="Анонсировать старт"),
        BotCommand(command="giveaway_status", description="Статистика розыгрыша"),
        BotCommand(command="giveaway_participants", description="Участники розыгрыша"),
        BotCommand(command="giveaway_finish", description="Завершить с подтверждением"),
        BotCommand(command="giveaway_announce_finish", description="Анонсировать завершение"),
        BotCommand(command="giveaway_reroll", description="Выбрать ещё одного победителя"),
        BotCommand(command="giveaway", description="Полная команда управления"),
    ]


def build_router(
    settings: Settings,
    storage: Storage,
    bot: Bot,
    twitch_state: TwitchChatState,
    refresh_stream_status: Callable[[], Awaitable[None]] | None = None,
    fetch_viewers: Callable[[], Awaitable[list[str]]] | None = None,
    configure_twitch_announcements: ConfigureTwitchAnnouncements | None = None,
    validate_twitch_chat_send: ValidateTwitchChatSend | None = None,
) -> Router:
    router = Router(name="giveaway")
    finish_confirmations: dict[int, tuple[int, float, tuple[object, ...]]] = {}
    public_command_timestamps: dict[tuple[int, str], float] = {}

    async def check_public_cooldown(message: Message, command: str) -> bool:
        if message.from_user is None or message.from_user.id == settings.owner_telegram_id:
            return True
        cooldown_seconds = PUBLIC_COMMAND_COOLDOWNS_SECONDS[command]
        key = (message.from_user.id, command)
        now = time.time()
        previous = public_command_timestamps.get(key)
        if previous is not None:
            retry_after = cooldown_seconds - int(now - previous)
            if retry_after > 0:
                await message.answer(
                    f"Слишком часто. Повторите команду через {retry_after} сек."
                )
                return False
        public_command_timestamps[key] = now
        return True

    async def send_link_instructions(message: Message) -> None:
        if message.chat.type != ChatType.PRIVATE or message.from_user is None:
            if not await check_public_cooldown(message, "link"):
                return
            await message.answer("Для безопасности привязку нужно начать в личных сообщениях с ботом.")
            return
        giveaway = await storage.active_giveaway()
        if giveaway is not None:
            registration = await storage.participant_stats(
                giveaway, message.from_user.id
            )
            if registration is not None:
                await message.answer(
                    f"Вы уже зарегистрированы в розыгрыше "
                    f"<b>{html.escape(giveaway.title)}</b>.\n\n"
                    "Telegram уже привязан к Twitch-аккаунту "
                    f"{twitch_profile_link(registration.twitch_login)}.\n"
                    "Повторно выполнять /link не нужно.",
                    parse_mode=ParseMode.HTML,
                )
                return
        if not await check_public_cooldown(message, "link"):
            return
        if giveaway is None:
            await message.answer(
                "Сейчас нет активного розыгрыша, поэтому регистрироваться пока некуда. "
                "Когда начнётся новый розыгрыш, снова выполните /link."
            )
            return
        if not twitch_state.connected:
            await message.answer(
                "Сейчас сборщик Twitch-чата переподключается, поэтому код пока не выдан. "
                "Повторите /link через несколько секунд."
            )
            return
        code = await storage.create_link_code(
            message.from_user.id,
            message.from_user.full_name,
            giveaway.id,
            message.from_user.username,
        )
        await message.answer(
            f"Регистрация в розыгрыше <b>{html.escape(giveaway.title)}</b>.\n\n"
            f"Откройте чат Twitch-канала {twitch_profile_link(settings.twitch_channel)} "
            "и в течение 10 минут отправьте:\n\n"
            f"<code>!link {code}</code>\n\n"
            "Не пересылайте этот код другим людям. Для следующего розыгрыша "
            "команду /link потребуется выполнить заново.",
            parse_mode=ParseMode.HTML,
        )

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject) -> None:
        if (command.args or "").strip().casefold() == "link":
            await send_link_instructions(message)
            return
        if not await check_public_cooldown(message, "start"):
            return
        await message.answer(
            "Я провожу розыгрыши для Twitch-чата.\n\n"
            "Чтобы участвовать в активном розыгрыше, отправьте /link, получите код и напишите его в Twitch-чате "
            "в виде <code>!link КОД</code>.\n\n"
            "Регистрацию через /link нужно проходить заново для каждого розыгрыша.\n\n"
            "Команда /status покажет, идёт ли сейчас учёт минут и сообщений.",
            parse_mode=ParseMode.HTML,
        )

    @router.message(Command("status"))
    async def tracking_status(message: Message) -> None:
        if message.chat.type != ChatType.PRIVATE:
            return
        if not await check_public_cooldown(message, "status"):
            return

        if refresh_stream_status is not None:
            try:
                await asyncio.wait_for(refresh_stream_status(), timeout=8)
            except TimeoutError:
                logger.warning("Принудительная проверка Twitch live для /status превысила 8 секунд")
            except Exception:
                logger.warning("Не удалось обновить Twitch live для /status", exc_info=True)

        giveaway = await storage.active_giveaway()
        participant_count: int | None = None
        if giveaway is not None:
            participant_count, _ = await storage.giveaway_status(
                giveaway,
                settings.twitch_excluded_logins,
                settings.telegram_excluded_usernames,
            )
        text = public_tracking_text(giveaway, twitch_state, participant_count)
        if giveaway is not None and message.from_user is not None:
            stats = await storage.participant_stats(giveaway, message.from_user.id)
            text += f"\n\n{personal_stats_text(giveaway, stats)}"
            username = (message.from_user.username or "").casefold().lstrip("@")
            if username and username in settings.telegram_excluded_usernames:
                text += (
                    "\n\n⚠️ Ваш Telegram-ник находится в списке исключений, "
                    "поэтому бот не добавит вас в кандидаты."
                )
        if message.from_user is not None and message.from_user.id == settings.owner_telegram_id:
            text += f"\n\n<b>Диагностика для владельца:</b>\n{twitch_diagnostics_text(settings, twitch_state)}"
        if giveaway is None:
            text += "\n\nКогда откроется новый розыгрыш, зарегистрируйтесь в нём через /link."
        await message.answer(text, parse_mode=ParseMode.HTML)

    @router.message(Command("link"))
    async def link(message: Message) -> None:
        await send_link_instructions(message)

    @router.message(Command("viewers"))
    async def viewers_command(message: Message) -> None:
        if not is_owner_command(message, settings):
            return
        if refresh_stream_status is not None:
            try:
                await asyncio.wait_for(refresh_stream_status(), timeout=8)
            except TimeoutError:
                logger.warning("Проверка Twitch live для /viewers превысила 8 секунд")
            except Exception:
                logger.warning("Не удалось обновить Twitch live для /viewers", exc_info=True)
        if twitch_state.stream_known and not twitch_state.stream_live:
            await message.answer("Стрим сейчас <b>offline</b>: зрителей текущего стрима нет.", parse_mode=ParseMode.HTML)
            return
        if fetch_viewers is None:
            await message.answer("Получение списка Twitch-чата не настроено.")
            return
        try:
            viewers = await asyncio.wait_for(fetch_viewers(), timeout=20)
        except TimeoutError:
            await message.answer("Twitch не успел вернуть список участников чата за 20 секунд.")
            return
        except Exception as error:
            logger.warning("Не удалось получить Twitch chatters", exc_info=True)
            await message.answer(
                "Не удалось получить список Twitch-чата.\n\n"
                f"<b>Причина:</b> {html.escape(str(error))}",
                parse_mode=ParseMode.HTML,
            )
            return
        if not viewers:
            await message.answer("Сейчас Twitch не видит ни одного подключённого участника чата.")
            return
        lines = [
            f"{index}. <code>@{html.escape(login)}</code>"
            for index, login in enumerate(viewers, start=1)
        ]
        lines.extend(
            [
                "",
                "<i>Twitch показывает подключённых к чату, а не точный список открытых видеоплееров. Обновление может происходить с задержкой.</i>",
            ]
        )
        await answer_long_html(
            message,
            f"👥 <b>Участники Twitch-чата сейчас:</b> {len(viewers)}",
            lines,
        )

    @router.message(Command("giveaway"))
    async def giveaway_command(message: Message, command: CommandObject) -> None:
        if not is_owner_command(message, settings):
            return

        raw_args = command.args or ""
        parts = raw_args.split()
        action = parts[0].lower() if parts else ""
        if action == "start":
            usage = (
                "Использование: "
                "<code>/giveaway start &lt;МИНУТЫ&gt; &lt;СООБЩЕНИЯ&gt; [ПОБЕДИТЕЛИ] [ИНТЕРВАЛ_СЕК] [МИН_УЧАСТНИКОВ] [--end ДД.ММ.ГГГГ ЧЧ:ММ] [НАЗВАНИЕ | НАГРАДА]</code>\n"
                "Пример: <code>/giveaway start 60 5 3 30 10 --end 31.12.2030 22:30 Розыгрыш ключей | Steam key</code>"
            )
            try:
                (
                    min_minutes,
                    min_messages,
                    winner_count,
                    message_interval_seconds,
                    min_participants,
                    end_at,
                    title,
                    prize,
                ) = parse_start_args(parts)
            except ValueError as error:
                await message.answer(
                    f"{usage}\n\n<b>Ошибка:</b> {html.escape(str(error))}",
                    parse_mode=ParseMode.HTML,
                )
                return
            try:
                giveaway = await storage.start_giveaway(
                    min_minutes,
                    min_messages,
                    winner_count,
                    title,
                    prize,
                    count_existing_presence=twitch_state.stream_live,
                    excluded_twitch_logins=settings.twitch_excluded_logins,
                    message_interval_seconds=message_interval_seconds,
                    min_participants=min_participants,
                    end_at=end_at,
                )
            except ValueError as error:
                await message.answer(str(error))
                return
            await message.answer(giveaway_created_text(giveaway), parse_mode=ParseMode.HTML)
            return

        if action in {"edit", "update", "set"}:
            giveaway = await storage.active_giveaway()
            if giveaway is None:
                await message.answer("Сейчас нет активного розыгрыша для изменения.")
                return
            try:
                parameter, value = parse_edit_args(raw_args)
                updated = await storage.update_active_giveaway(
                    **{parameter: value}
                )
            except ValueError as error:
                await message.answer(
                    f"{giveaway_edit_usage()}\n\n<b>Ошибка:</b> {html.escape(str(error))}",
                    parse_mode=ParseMode.HTML,
                )
                return
            if updated is None:
                await message.answer("Активный розыгрыш уже завершён и не был изменён.")
                return
            changed = giveaway_parameter_value(
                giveaway, parameter
            ) != giveaway_parameter_value(updated, parameter)
            confirmation_cancelled = False
            if changed and message.from_user is not None:
                confirmation_cancelled = (
                    finish_confirmations.pop(message.from_user.id, None) is not None
                )
            await message.answer(
                giveaway_updated_text(
                    giveaway,
                    updated,
                    parameter,
                    finish_confirmation_cancelled=confirmation_cancelled,
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        if action in {"announce_start", "announce-start", "announce"}:
            giveaway = await storage.active_giveaway()
            if giveaway is None:
                await message.answer("Нет активного розыгрыша для анонса.")
                return
            bot_info = await bot.get_me()
            if not bot_info.username:
                await message.answer("У Telegram-бота не задан username, поэтому ссылку создать нельзя.")
                return
            await publish_html(
                message,
                bot,
                settings,
                giveaway_start_announcement(giveaway, bot_info.username),
            )
            return

        if action in {"twitch_announce", "twitch-announce"}:
            giveaway = await storage.active_giveaway()
            if giveaway is None:
                await message.answer(
                    "Нет активного розыгрыша для Twitch-анонсов."
                )
                return
            mode = parts[1].casefold() if len(parts) >= 2 else "status"
            interval_minutes: int | None = None
            if mode.isdigit():
                if len(parts) != 2:
                    await message.answer(
                        "Использование: <code>/twitch_announce on 15</code>.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                interval_minutes = int(mode)
                mode = "on"
            if mode in {"status", "состояние"} and len(parts) in {1, 2}:
                await message.answer(
                    twitch_announcement_status_text(giveaway, twitch_state),
                    parse_mode=ParseMode.HTML,
                )
                return
            if mode in {"on", "enable", "вкл"}:
                if interval_minutes is None:
                    if len(parts) >= 3 and parts[2].isdigit():
                        interval_minutes = int(parts[2])
                    elif len(parts) >= 3:
                        await message.answer(
                            "Интервал указывается целым числом минут. Например: "
                            "<code>/twitch_announce on 15</code>.",
                            parse_mode=ParseMode.HTML,
                        )
                        return
                    else:
                        interval_minutes = 15
                if len(parts) > 3 or not 1 <= interval_minutes <= 1440:
                    await message.answer(
                        "Интервал Twitch-анонсов должен быть от 1 до 1440 минут."
                    )
                    return
                if validate_twitch_chat_send is not None:
                    try:
                        await asyncio.wait_for(validate_twitch_chat_send(), timeout=10)
                    except TimeoutError:
                        await message.answer(
                            "Twitch не успел проверить право chat:edit за 10 секунд. "
                            "Попробуйте включить анонсы ещё раз."
                        )
                        return
                    except Exception as error:
                        await message.answer(
                            "Не удалось включить Twitch-анонсы.\n\n"
                            f"<b>Причина:</b> {html.escape(str(error))}",
                            parse_mode=ParseMode.HTML,
                        )
                        return
                try:
                    if configure_twitch_announcements is not None:
                        updated = await configure_twitch_announcements(
                            True, interval_minutes
                        )
                    else:
                        updated = await storage.configure_twitch_announcements(
                            enabled=True, interval_minutes=interval_minutes
                        )
                except ValueError as error:
                    await message.answer(str(error))
                    return
                if updated is None:
                    await message.answer("Активный розыгрыш уже завершён.")
                    return
                await message.answer(
                    "✅ Twitch-анонсы включены.\n"
                    f"Периодичность: каждые <b>{interval_minutes} мин</b>.\n"
                    "Первый анонс будет отправлен, когда стрим live и Twitch-чат подключён.\n\n"
                    "OAuth-токену требуется право <code>chat:edit</code>.",
                    parse_mode=ParseMode.HTML,
                )
                return
            if mode in {"off", "disable", "выкл"} and len(parts) == 2:
                if configure_twitch_announcements is not None:
                    updated = await configure_twitch_announcements(False, None)
                else:
                    updated = await storage.configure_twitch_announcements(
                        enabled=False
                    )
                if updated is None:
                    await message.answer("Активный розыгрыш уже завершён.")
                    return
                await message.answer("Twitch-анонсы розыгрыша выключены.")
                return
            await message.answer(
                "Использование:\n"
                "<code>/twitch_announce on 15</code> — включить каждые 15 минут\n"
                "<code>/twitch_announce off</code> — выключить\n"
                "<code>/twitch_announce status</code> — показать состояние",
                parse_mode=ParseMode.HTML,
            )
            return

        if action == "status":
            giveaway = await storage.active_giveaway()
            if giveaway is None:
                await message.answer("Сейчас нет активного розыгрыша.")
                return
            tracked, currently_qualified = await storage.giveaway_status(
                giveaway,
                settings.twitch_excluded_logins,
                settings.telegram_excluded_usernames,
            )
            await message.answer(
                f"📊 <b>Текущий статус:</b> {html.escape(giveaway.title)}\n"
                + (f"Награда: <b>{html.escape(giveaway.prize)}</b>\n" if giveaway.prize else "")
                + (
                    f"Плановое завершение: <b>{giveaway_datetime_text(giveaway.end_at)}</b>\n"
                    if giveaway.end_at is not None
                    else ""
                )
                + (
                f"Условия: {giveaway.min_seconds // 60} мин, {giveaway.min_messages} сообщений\n"
                f"Интервал сообщений: {giveaway.message_interval_seconds} сек\n"
                f"Победителей: {giveaway.winner_count}\n"
                "Для выбора победителей необходимо не менее "
                f"{giveaway.min_participants} допущенных участников.\n"
                f"Зарегистрировались в розыгрыше: {tracked}\n"
                f"Уже выполнили технические условия: {currently_qualified}\n\n"
                f"{twitch_announcement_status_text(giveaway, twitch_state)}\n\n"
                "Финальное число может быть меньше: при завершении бот исключит тех, "
                "кто не подписан на нужный Telegram-канал/чат."
                ),
                parse_mode=ParseMode.HTML,
            )
            return

        if action in {"participants", "members", "list"}:
            split_args = raw_args.split(maxsplit=1)
            title_query = split_args[1].strip() if len(split_args) > 1 else ""
            if title_query:
                giveaway = await storage.latest_giveaway_by_title(title_query)
                list_kind = "найденного"
                if giveaway is None:
                    await message.answer(
                        f"Не нашёл розыгрыш с названием <b>{html.escape(title_query)}</b>.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
            else:
                giveaway = await storage.active_giveaway()
                list_kind = "активного"
                if giveaway is None:
                    giveaway = await storage.latest_finished_giveaway()
                    list_kind = "последнего завершённого"
            if giveaway is None:
                await message.answer("Пока нет розыгрыша, для которого можно показать участников.")
                return

            participants = await storage.giveaway_participants(
                giveaway,
                settings.twitch_excluded_logins,
                settings.telegram_excluded_usernames,
            )
            if not participants:
                await message.answer(
                    f"В {list_kind} розыгрыше <b>{html.escape(giveaway.title)}</b> пока нет участников.",
                    parse_mode=ParseMode.HTML,
                )
                return

            lines = []
            for index, participant in enumerate(participants, start=1):
                telegram = "Telegram: не привязан"
                if participant.telegram_user_id is not None and participant.telegram_name is not None:
                    telegram_name = (
                        f"@{html.escape(participant.telegram_username)}"
                        if participant.telegram_username
                        else html.escape(participant.telegram_name)
                    )
                    telegram = (
                        f"Telegram: <a href=\"tg://user?id={participant.telegram_user_id}\">"
                        f"{telegram_name}</a>"
                    )
                lines.append(
                    f"{index}. Twitch: <code>{html.escape(participant.twitch_login)}</code> "
                    f"- {duration_text(participant.seconds)}, {participant.messages} сообщений; "
                    f"{telegram}"
                )
            header = (
                f"<b>Участники {list_kind} розыгрыша:</b> {html.escape(giveaway.title)}\n"
                + (f"Награда: <b>{html.escape(giveaway.prize)}</b>\n" if giveaway.prize else "")
                + (
                f"Условия: {giveaway.min_seconds // 60} мин, {giveaway.min_messages} сообщений; "
                f"интервал сообщений: {giveaway.message_interval_seconds} сек; "
                f"победителей: {giveaway.winner_count}; "
                f"для выбора победителей нужно допущенных участников: "
                f"не менее {giveaway.min_participants}\n"
                )
            )
            await answer_long_html(message, header, lines)
            return

        if action in {"finish", "reroll"}:
            force_finish = False
            if action == "finish":
                giveaway = await storage.active_giveaway()
                if giveaway is None:
                    if message.from_user is not None:
                        finish_confirmations.pop(message.from_user.id, None)
                    await message.answer("Нет активного розыгрыша для завершения.")
                    return
                owner_id = message.from_user.id if message.from_user is not None else 0
                finish_options = {part.casefold() for part in parts[1:]}
                if not finish_options.issubset({"confirm", "cancel", "force"}) or (
                    "confirm" in finish_options and "cancel" in finish_options
                ):
                    await message.answer(
                        "Для обычного подтверждения используйте "
                        "<code>/giveaway_finish confirm</code>, для принудительного выбора "
                        "без достижения минимума — "
                        "<code>/giveaway_finish force confirm</code>, для отмены — "
                        "<code>/giveaway_finish cancel</code>.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                force_finish = "force" in finish_options
                confirmation = "confirm" if "confirm" in finish_options else ""
                if "cancel" in finish_options:
                    finish_confirmations.pop(owner_id, None)
                    await message.answer(
                        f"Завершение розыгрыша <b>{html.escape(giveaway.title)}</b> отменено.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                if confirmation != "confirm":
                    finish_confirmations[owner_id] = (
                        giveaway.id,
                        time.time() + 120,
                        giveaway_rules_signature(giveaway),
                    )
                    if force_finish:
                        confirmation_text = (
                            "Будет проигнорирован только минимальный размер списка участников. "
                            "Требования по времени, сообщениям и членству в Telegram сохраняются.\n\n"
                            "В течение двух минут отправьте "
                            "<code>/giveaway_finish force confirm</code>."
                        )
                    else:
                        confirmation_text = (
                            "Если установленный минимум участников не достигнут, розыгрыш "
                            "завершится без победителей.\n\n"
                            "В течение двух минут отправьте "
                            "<code>/giveaway_finish confirm</code>.\n"
                            "Чтобы всё равно выбрать победителей из допущенных участников: "
                            "<code>/giveaway_finish force confirm</code>."
                        )
                    await message.answer(
                        f"⚠️ <b>Подтвердите завершение розыгрыша:</b> "
                        f"{html.escape(giveaway.title)}\n\n"
                        "После завершения учёт остановится и, если условия выполнены, "
                        "будут выбраны победители.\n\n"
                        f"{confirmation_text}\n"
                        "Для отмены: <code>/giveaway_finish cancel</code>.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                pending = finish_confirmations.pop(owner_id, None)
                if (
                    pending is None
                    or pending[0] != giveaway.id
                    or pending[1] < time.time()
                    or pending[2] != giveaway_rules_signature(giveaway)
                ):
                    await message.answer(
                        "Нет действующего подтверждения, изменились параметры розыгрыша "
                        "или истекли две минуты. "
                        "Сначала снова отправьте <code>/giveaway_finish</code>.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                try:
                    draw_result = await create_persisted_draw(
                        bot,
                        settings,
                        storage,
                        giveaway,
                        force=force_finish,
                    )
                except TelegramMembershipCheckError:
                    await message.answer(
                        "Не удалось проверить членство участников в Telegram. "
                        "Розыгрыш остаётся активным, статистика и регистрации сохранены. "
                        "Повторите <code>/giveaway_finish</code> немного позже.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                except (ValueError, RuntimeError):
                    logger.warning(
                        "Не удалось атомарно завершить розыгрыш %s",
                        giveaway.id,
                        exc_info=True,
                    )
                    await message.answer(
                        "Не удалось зафиксировать жеребьёвку: параметры или список "
                        "регистраций изменились во время проверки. Если розыгрыш ещё "
                        "активен, снова выполните <code>/giveaway_finish</code>. "
                        "Неподтверждённые результаты не сохранялись.",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                if configure_twitch_announcements is not None:
                    await configure_twitch_announcements(False, None)
            else:
                giveaway = await storage.latest_finished_giveaway()
                if giveaway is None:
                    await message.answer("Нет завершённого розыгрыша для перевыбора.")
                    return
                try:
                    draw_result = await create_persisted_draw(
                        bot,
                        settings,
                        storage,
                        giveaway,
                        reroll=True,
                    )
                except TelegramMembershipCheckError:
                    await message.answer(
                        "Не удалось проверить членство участников в Telegram. "
                        "Новый раунд не создавался; повторите команду позже."
                    )
                    return
                except (ValueError, RuntimeError):
                    logger.info(
                        "Повторная жеребьёвка розыгрыша %s не состоялась",
                        giveaway.id,
                        exc_info=True,
                    )
                    await message.answer(
                        "Подходящих участников не осталось, либо список регистраций "
                        "изменился во время проверки. Повторная жеребьёвка не "
                        "проводилась, новый раунд не создавался."
                    )
                    return

            winners = [draw_entry_candidate(entry) for entry in draw_result.winners]
            report_status = await send_persisted_giveaway_report(
                bot,
                storage,
                giveaway,
                settings.owner_telegram_id,
            )
            report_warning = (
                "\n\n⚠️ XLSX-отчёт не удалось отправить владельцу. "
                "Результаты жеребьёвки сохранены; файл можно повторно получить через "
                "анонс завершения."
                if report_status is False
                else ""
            )
            if not winners:
                if draw_result.round.eligible_count == 0:
                    reason = (
                        "нет ни одного допущенного участника, который выполнил "
                        "условия и состоит в нужном Telegram-канале/чате"
                    )
                else:
                    reason = (
                        f"допущенных участников <b>{draw_result.round.eligible_count}</b>, "
                        "а для выбора требовалось не менее "
                        f"<b>{draw_result.round.min_participants_required}</b>"
                    )
                await message.answer(
                    f"Розыгрыш завершён без победителей: {reason}.\n\n"
                    "Случайные числа и итоговая статистика всех зарегистрированных "
                    "сохранены в XLSX. Чтобы опубликовать завершение и таблицу в "
                    "канал/чат, используйте <code>/giveaway announce_finish</code>."
                    f"{report_warning}",
                    parse_mode=ParseMode.HTML,
                )
                return

            failed_notifications = await notify_winners(bot, giveaway, winners)
            prefix = "🏆 <b>Победители</b>" if action == "finish" else "🔄 <b>Новый победитель</b>"
            winner_lines = format_winner_lines(winners)
            winner_lines.append("")
            if failed_notifications:
                winner_lines.append(
                    "⚠️ Не удалось отправить личное сообщение "
                    f"{len(failed_notifications)} из {len(winners)} победителей. "
                    "Возможно, пользователь заблокировал бота."
                )
            else:
                winner_lines.append("✅ Личные уведомления победителям отправлены.")
            winner_lines.append(
                "Кандидатов после проверки Telegram-канала/чата: "
                f"{draw_result.round.eligible_count}."
            )
            if (
                action == "finish"
                and force_finish
                and not draw_result.round.min_participants_met
            ):
                winner_lines.append(
                    "⚠️ Минимум участников не достигнут: победители выбраны "
                    "в принудительном режиме."
                )
            if (
                action == "finish"
                and len(winners) < draw_result.round.requested_winner_count
            ):
                winner_lines.append(
                    "Запрошено победителей: "
                    f"{draw_result.round.requested_winner_count}, "
                    f"подходящих кандидатов нашлось: {len(winners)}."
                )
            winner_lines.append(
                "📊 Случайные числа и статистика всех зарегистрированных "
                f"сохранены в XLSX (раунд {draw_result.round.round_number})."
            )
            if report_status is False:
                winner_lines.append(
                    "⚠️ XLSX не удалось отправить владельцу, но результаты сохранены."
                )
            await answer_long_html(message, f"{prefix}: {html.escape(giveaway.title)}", winner_lines)
            if action == "finish":
                finish_mode = " принудительно" if force_finish else ""
                await message.answer(
                    f"Розыгрыш завершён{finish_mode}, победители выбраны. "
                    "Чтобы опубликовать результат в канал/чат, "
                    "используйте <code>/giveaway announce_finish</code>.",
                    parse_mode=ParseMode.HTML,
                )
            return

        if action in {"announce_finish", "announce-finish", "announce_result", "announce-results"}:
            split_args = raw_args.split(maxsplit=1)
            title_query = split_args[1].strip() if len(split_args) > 1 else ""
            if title_query:
                giveaway = await storage.latest_finished_giveaway_by_title(title_query)
            else:
                giveaway = await storage.latest_finished_giveaway()
            if giveaway is None:
                await message.answer("Нет завершённого розыгрыша для анонса.")
                return
            winners = await storage.recorded_winners(giveaway)
            header, lines = giveaway_finish_announcement(giveaway, winners)
            await publish_long_html(
                message,
                bot,
                settings,
                header,
                lines,
            )
            report_status = await send_persisted_giveaway_report(
                bot,
                storage,
                giveaway,
                settings.telegram_required_chat_id,
            )
            if report_status is False:
                await message.answer(
                    "Текстовый анонс опубликован, но XLSX-отчёт отправить не удалось. "
                    "Сохранённые результаты не изменились; повторите команду позже."
                )
            elif report_status is None:
                await message.answer(
                    "Для этого старого розыгрыша случайные числа ещё не сохранялись, "
                    "поэтому XLSX-протокол недоступен. Текстовый результат опубликован."
                )
            return

        await message.answer(
            "Команды:\n"
            "<code>/giveaway start &lt;МИНУТЫ&gt; &lt;СООБЩЕНИЯ&gt; [ПОБЕДИТЕЛИ] [ИНТЕРВАЛ_СЕК] [МИН_УЧАСТНИКОВ] [--end ДД.ММ.ГГГГ ЧЧ:ММ] [НАЗВАНИЕ | НАГРАДА]</code>\n"
            "<code>/giveaway edit &lt;ПАРАМЕТР&gt; &lt;ЗНАЧЕНИЕ&gt;</code>\n"
            "<code>/giveaway announce_start</code>\n"
            "<code>/giveaway status</code>\n"
            "<code>/giveaway participants [НАЗВАНИЕ]</code>\n"
            "<code>/giveaway twitch_announce on 15|off|status</code>\n"
            "<code>/giveaway finish</code>\n"
            "<code>/giveaway finish force</code>\n"
            "<code>/giveaway finish force confirm</code>\n"
            "<code>/giveaway announce_finish [НАЗВАНИЕ]</code>\n"
            "<code>/giveaway reroll</code>",
            parse_mode=ParseMode.HTML,
        )

    @router.message(
        Command(
            "giveaway_create",
            "giveaway_edit",
            "giveaway_announce_start",
            "giveaway_status",
            "giveaway_participants",
            "giveaway_finish",
            "giveaway_announce_finish",
            "giveaway_reroll",
            "twitch_announce",
        )
    )
    async def giveaway_shortcut(message: Message, command: CommandObject) -> None:
        if not is_owner_command(message, settings):
            return
        shortcut_map = {
            "giveaway_create": "start",
            "giveaway_edit": "edit",
            "giveaway_announce_start": "announce_start",
            "giveaway_status": "status",
            "giveaway_participants": "participants",
            "giveaway_finish": "finish",
            "giveaway_announce_finish": "announce_finish",
            "giveaway_reroll": "reroll",
            "twitch_announce": "twitch_announce",
        }
        command_name = command.command
        if command_name is None:
            return
        action = shortcut_map.get(command_name)
        if action is None:
            return
        args = f"{action} {command.args or ''}".strip()
        await giveaway_command(message, CommandObject(command="giveaway", args=args))

    return router


async def run() -> None:
    settings = Settings.from_env()
    storage = Storage(settings.database_path)
    await storage.connect()
    await storage.reset_chat_session(preserve_elapsed=False)
    bot = Bot(settings.telegram_bot_token)
    twitch_state = TwitchChatState()
    dispatcher = Dispatcher()
    twitch_announcer: TwitchGiveawayAnnouncer | None = None

    async def notify_linked(telegram_user_id: int, _name: str) -> None:
        try:
            await bot.send_message(
                telegram_user_id,
                "✅ Twitch-аккаунт подтверждён. Вы зарегистрированы в текущем розыгрыше. "
                "Проверить свои минуты и сообщения можно командой /status.",
            )
        except TelegramAPIError:
            logger.warning("Не удалось подтвердить привязку Telegram %s", telegram_user_id)

    async def on_live_changed(is_live: bool) -> None:
        await handle_stream_state_change(
            is_live, bot, settings, storage, twitch_state
        )
        if is_live and twitch_announcer is not None:
            twitch_announcer.wake()

    twitch = TwitchChat(
        channel=settings.twitch_channel,
        bot_login=settings.twitch_bot_login,
        oauth_token=settings.twitch_oauth_token,
        storage=storage,
        notify_linked=notify_linked,
        state=twitch_state,
        tracking_enabled=lambda: twitch_state.stream_live,
        excluded_logins=settings.twitch_excluded_logins,
    )
    live_monitor = TwitchLiveMonitor(
        channel=settings.twitch_channel,
        oauth_token=settings.twitch_oauth_token,
        state=twitch_state,
        on_live_changed=on_live_changed,
        poll_interval_seconds=settings.twitch_live_check_interval_seconds,
    )
    bot_info = await bot.get_me()
    if not bot_info.username:
        raise RuntimeError("У Telegram-бота не задан username")
    twitch_announcer = TwitchGiveawayAnnouncer(
        storage=storage,
        twitch_state=twitch_state,
        send_chat_message=twitch.send_chat_message,
        bot_username=bot_info.username,
    )
    dispatcher.include_router(
        build_router(
            settings,
            storage,
            bot,
            twitch_state,
            refresh_stream_status=live_monitor.refresh_if_stale,
            fetch_viewers=live_monitor.fetch_chatters,
            configure_twitch_announcements=twitch_announcer.configure,
            validate_twitch_chat_send=live_monitor.require_chat_edit_scope,
        )
    )
    try:
        await live_monitor.refresh()
    except Exception:
        logger.warning(
            "Не удалось выполнить начальную проверку статуса Twitch-стрима",
            exc_info=True,
        )
    twitch_task = asyncio.create_task(twitch.run_forever(), name="twitch-chat")
    live_monitor_task = asyncio.create_task(live_monitor.run_forever(), name="twitch-live")
    twitch_announcer_task = asyncio.create_task(
        twitch_announcer.run_forever(), name="twitch-giveaway-announcer"
    )
    try:
        logger.info("Telegram-бот запущен для Telegram-канала/чата %s", settings.telegram_required_chat_id)
        await bot.set_my_commands(
            public_bot_commands()
        )
        await bot.set_my_commands(
            owner_bot_commands(),
            scope=BotCommandScopeChat(chat_id=settings.owner_telegram_id),
        )
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        twitch_announcer.stop()
        twitch_announcer_task.cancel()
        with suppress(asyncio.CancelledError):
            await twitch_announcer_task
        twitch.stop()
        live_monitor.stop()
        twitch_task.cancel()
        live_monitor_task.cancel()
        with suppress(asyncio.CancelledError):
            await twitch_task
        with suppress(asyncio.CancelledError):
            await live_monitor_task
        await twitch.wait_for_notices()
        await bot.session.close()
        await storage.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
