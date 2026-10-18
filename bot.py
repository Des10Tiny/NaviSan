"""Telegram-бот NaviSan (ADR-0013).

Единственный интерфейс к ядру NaviSan. Следит за папкой Inbox,
запускает pipeline обработки и ведёт диалог с владельцем:
- показывает кандидатов обложек → ждёт выбора
- показывает Plan изменений → ждёт подтверждения перед Apply
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from telegram import (
    Bot,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from covers import CoverCandidate, download_cover, search_covers
from sanitizer import TagSanitizer
from surgeon import FLACSurgeon
from renamer import FileSystemManager
from lyricist import LyricManager

load_dotenv()

# ── Конфигурация ─────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID: int = int(os.environ["NAVISAN_OWNER_ID"])
INBOX: Path = Path(os.environ["NAVISAN_INBOX"])
LIBRARY: Path = Path(os.environ["NAVISAN_LIBRARY"])
DB_PATH: str = os.environ.get("NAVISAN_DB", "navidrome.db")
ALIASES_PATH: str = os.environ.get("NAVISAN_ALIASES", "aliases.json")

# ── Состояние сессии ──────────────────────────────────────────────────────────

@dataclass
class AlbumSession:
    """Контекст одного незавершённого альбома.

    Живёт в памяти; при перезапуске бота теряется — это осознанно (ADR-0013).
    """

    folder: Path                        # папка в Inbox / Staging
    candidates: list[CoverCandidate] = field(default_factory=list)
    chosen_cover: CoverCandidate | None = None


# Один активный сеанс на пользователя. Для личного бота этого достаточно.
_sessions: dict[int, AlbumSession] = {}


# ── Авторизация ───────────────────────────────────────────────────────────────

def _owner_only(func):
    """Декоратор — молча игнорирует любые запросы не от владельца."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id != OWNER_ID:
            return
        return await func(update, ctx)
    return wrapper


# ── Ядро NaviSan ──────────────────────────────────────────────────────────────

def _build_pipeline() -> tuple[FLACSurgeon, FileSystemManager, LyricManager]:
    sanitizer = TagSanitizer(aliases_path=ALIASES_PATH, db_path=DB_PATH)
    surgeon = FLACSurgeon(sanitizer=sanitizer)
    renamer = FileSystemManager()
    lyricist = LyricManager()
    return surgeon, renamer, lyricist


def _run_pipeline(folder: Path) -> list[str]:
    """Запускает теги→переименование→тексты для всех треков в папке.

    Возвращает список строк-отчёта (по одной на трек).
    """
    surgeon, renamer, lyricist = _build_pipeline()
    report: list[str] = []

    flac_files = sorted(folder.rglob("*.flac"))
    if not flac_files:
        return ["⚠️ FLAC-файлы не найдены"]

    for flac in flac_files:
        surgeon.heal_file(str(flac))
        new_path = renamer.rename_file(str(flac))
        lrc_status = lyricist.process_file(new_path)
        report.append(f"• `{Path(new_path).name}` — {lrc_status}")

    # Переименовываем папку альбома после обработки треков
    new_folder = renamer.rename_album_folder(str(folder))
    report.append(f"📁 Папка: `{Path(new_folder).name}`")

    return report


# ── Хелперы ───────────────────────────────────────────────────────────────────

def _album_info(folder: Path) -> tuple[str, str]:
    """Возвращает (artist, album) из первого FLAC в папке."""
    from mutagen.flac import FLAC

    for f in sorted(folder.rglob("*.flac")):
        try:
            audio = FLAC(str(f))
            artist = (
                audio.get("albumartist", audio.get("artist", [""]))[0].split("&")[0].strip()
            )
            album = audio.get("album", [""])[0]
            return artist, album
        except Exception:
            continue
    return folder.name, ""


def _cover_caption(c: CoverCandidate, idx: int) -> str:
    parts = [f"*{idx}.* [{c.source}]"]
    if c.title:
        parts.append(c.title)
    if c.artist:
        parts.append(f"— {c.artist}")
    if c.date:
        parts.append(f"({c.date})")
    if c.tracks:
        parts.append(f"· {c.tracks} tracks")
    return " ".join(parts)


def _date_is_more_specific(new_date: str, existing_date: str) -> bool:
    """True, если *new_date* содержит больше информации, чем *existing_date*.

    Критерий: длина строки. "2019-06-21" (10) > "2019" (4).
    """
    return len(new_date.strip()) > len(existing_date.strip())


def _apply_date_to_flacs(folder: Path, date_str: str) -> None:
    """Записывает дату в теги DATE/YEAR всех FLAC в папке (если новая специфичнее)."""
    from mutagen.flac import FLAC

    year = date_str[:4]
    for f in folder.rglob("*.flac"):
        try:
            audio = FLAC(str(f))
            existing = audio.get("date", audio.get("year", [""]))[0]
            if existing and not _date_is_more_specific(date_str, existing):
                continue
            audio["date"] = [date_str]
            audio["year"] = [year]
            audio.save()
        except Exception as e:
            log.warning("Не удалось записать дату в %s: %s", f.name, e)


# ── Обработчики команд ────────────────────────────────────────────────────────

@_owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "NaviSan бот запущен ✅\n\n"
        "Команды:\n"
        "/process `<путь>` — обработать папку с альбомом\n"
        "/cover `<путь>` — найти обложку для папки\n"
        "/status — текущая сессия",
        parse_mode="Markdown",
    )


@_owner_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(OWNER_ID)
    if not session:
        await update.message.reply_text("Активных сессий нет.")
        return
    chosen = session.chosen_cover.source if session.chosen_cover else "не выбрана"
    await update.message.reply_text(
        f"📂 Папка: `{session.folder}`\n"
        f"🖼️ Обложка: {chosen}\n"
        f"🔢 Кандидатов: {len(session.candidates)}",
        parse_mode="Markdown",
    )


@_owner_only
async def cmd_cover(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Поиск и выбор обложки для указанной папки."""
    if not ctx.args:
        await update.message.reply_text("Укажи путь к папке: /cover `/mnt/music/inbox/Artist - Album`")
        return

    folder = Path(" ".join(ctx.args))
    if not folder.exists() or not folder.is_dir():
        await update.message.reply_text(f"❌ Папка не найдена: `{folder}`", parse_mode="Markdown")
        return

    artist, album = _album_info(folder)
    if not artist and not album:
        await update.message.reply_text("❌ Не удалось прочитать теги из FLAC-файлов.")
        return

    # Создаём/перезаписываем сессию
    _sessions[OWNER_ID] = AlbumSession(folder=folder)

    await update.message.reply_text(
        f"🔍 Ищу обложки для *{artist}* — *{album}*…",
        parse_mode="Markdown",
    )

    # Поиск в отдельном потоке, чтобы не блокировать event loop
    candidates: list[CoverCandidate] = await ctx.application.run_in_executor(
        None, lambda: search_covers(artist, album)
    )

    if not candidates:
        await update.message.reply_text("😕 Обложки не найдены.")
        return

    _sessions[OWNER_ID].candidates = candidates

    await _send_cover_picker(update, ctx, candidates)


@_owner_only
async def cmd_process(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Запустить полный pipeline обработки для папки."""
    if not ctx.args:
        await update.message.reply_text("Укажи путь к папке: /process `/mnt/music/inbox/Artist - Album`")
        return

    folder = Path(" ".join(ctx.args))
    if not folder.exists() or not folder.is_dir():
        await update.message.reply_text(f"❌ Папка не найдена: `{folder}`", parse_mode="Markdown")
        return

    await update.message.reply_text(f"⚙️ Обрабатываю `{folder.name}`…", parse_mode="Markdown")

    report_lines = await ctx.application.run_in_executor(
        None, lambda: _run_pipeline(folder)
    )

    report = "\n".join(report_lines)
    await update.message.reply_text(f"✅ Готово:\n{report}", parse_mode="Markdown")


# ── Cover Picker UI ───────────────────────────────────────────────────────────

async def _send_cover_picker(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    candidates: list[CoverCandidate],
) -> None:
    """Отправляет MediaGroup с превью и Inline-кнопками выбора."""
    # Telegram MediaGroup принимает максимум 10 фото
    display = candidates[:10]

    media: list[InputMediaPhoto] = []
    for i, c in enumerate(display, 1):
        caption = _cover_caption(c, i)
        media.append(InputMediaPhoto(media=c.small_url, caption=caption, parse_mode="Markdown"))

    try:
        await ctx.bot.send_media_group(chat_id=update.effective_chat.id, media=media)
    except Exception as e:
        log.error("Ошибка отправки MediaGroup: %s", e)
        await update.message.reply_text(f"❌ Не удалось отправить превью: {e}")
        return

    # Кнопки
    buttons = [
        InlineKeyboardButton(str(i), callback_data=f"cover:{i - 1}")
        for i in range(1, len(display) + 1)
    ]
    # Кнопки по 5 в ряд
    keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    keyboard.append([InlineKeyboardButton("❌ Пропустить", callback_data="cover:skip")])

    await ctx.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"Выбери обложку (1–{len(display)}) или пропусти:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_cover_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатия кнопки выбора обложки."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != OWNER_ID:
        return

    session = _sessions.get(OWNER_ID)
    if not session:
        await query.edit_message_text("⚠️ Сессия устарела. Начни заново.")
        return

    data = query.data  # "cover:3" или "cover:skip"
    _, payload = data.split(":", 1)

    if payload == "skip":
        await query.edit_message_text("⏭️ Обложка пропущена.")
        return

    idx = int(payload)
    if idx >= len(session.candidates):
        await query.edit_message_text("❌ Некорректный номер.")
        return

    chosen = session.candidates[idx]
    session = AlbumSession(
        folder=session.folder,
        candidates=session.candidates,
        chosen_cover=chosen,
    )
    _sessions[OWNER_ID] = session

    await query.edit_message_text(
        f"⏳ Скачиваю `{chosen.source}` — {chosen.title}…", parse_mode="Markdown"
    )

    dest = session.folder / "cover.jpg"
    try:
        await ctx.application.run_in_executor(
            None, lambda: download_cover(chosen.big_url, dest)
        )
    except Exception as e:
        await ctx.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Ошибка скачивания: {e}",
        )
        return

    # Записываем дату в теги, если она более специфична
    date_note = ""
    if chosen.date:
        _apply_date_to_flacs(session.folder, chosen.date)
        date_note = f"\n📅 Дата из источника: `{chosen.date}` → записана в теги."

    await ctx.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"✅ Обложка сохранена: `{dest}`{date_note}\n\n"
             f"Теперь запусти /process `{session.folder}` для полной обработки.",
        parse_mode="Markdown",
    )


# ── Точка входа ───────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cover", cmd_cover))
    app.add_handler(CommandHandler("process", cmd_process))
    app.add_handler(CallbackQueryHandler(on_cover_callback, pattern=r"^cover:"))

    log.info("NaviSan бот запускается (owner_id=%d)", OWNER_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
