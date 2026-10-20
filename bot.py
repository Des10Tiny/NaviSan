"""Telegram-бот NaviSan (ADR-0013).

Единственный интерфейс к ядру NaviSan. Следит за папкой Inbox (watcher),
запускает pipeline обработки через Staging и ведёт диалог с владельцем:

  Inbox Watcher обнаруживает новый релиз
      ↓
  Бот ищет обложки → присылает нативный Telegram Slideshow (свайп влево/вправо)
      ↓
  Владелец свайпает и нажимает кнопку с номером обложки
      ↓
  Бот удаляет слайдшоу и присылает 1 статусное сообщение,
  которое превращается в финальный отчёт без спама в чат.
"""

from __future__ import annotations

import asyncio
import logging
import os
from enum import Enum, auto
from pathlib import Path
from dataclasses import dataclass, field

from dotenv import load_dotenv
from mutagen.flac import FLAC
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from covers import CoverCandidate, search_covers
from lyricist import LyricManager, has_timestamps, preview_lyrics
from pipeline import (
    album_info,
    apply_cover,
    apply_pipeline,
    cleanup_inbox,
    copy_to_staging,
    count_flacs,
    move_to_library,
    update_album_cover,
)
from watcher import watch_inbox

load_dotenv()

# ── Конфигурация ─────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID: int = int(os.environ["NAVISAN_OWNER_ID"])
INBOX: Path = Path(os.environ.get("NAVISAN_INBOX", "/inbox"))
STAGING: Path = Path(os.environ.get("NAVISAN_STAGING", "/staging"))
LIBRARY: Path = Path(os.environ.get("NAVISAN_LIBRARY", "/library"))
DB_PATH: str = os.environ.get("NAVISAN_DB", "navidrome.db")
ALIASES_PATH: str = os.environ.get("NAVISAN_ALIASES", "aliases.json")


# ── Состояние сессии (State Machine) ──────────────────────────────────────────

class SessionState(Enum):
    """Этапы обработки одного альбома."""
    COVER_PENDING = auto()   # слайдшоу показано, ждём выбора
    PROCESSING = auto()      # pipeline работает
    DONE = auto()            # завершено


PAGE_SIZE = 4


@dataclass
class AlbumSession:
    """Контекст обработки одного альбома."""
    state: SessionState
    folder: Path                        # папка в Inbox или Library
    staging_folder: Path | None = None  # копия в Staging
    artist: str = ""
    album: str = ""
    n_tracks: int = 0
    candidates: list[CoverCandidate] = field(default_factory=list)
    chosen_cover: CoverCandidate | None = None
    picker_message_id: int | None = None # ID сообщения со слайдшоу
    is_recover: bool = False            # True для смены обложки в Library через /cover
    page: int = 0


# Один активный сеанс на пользователя
_sessions: dict[int, AlbumSession] = {}


# ── Авторизация ───────────────────────────────────────────────────────────────

def _owner_only(func):
    """Декоратор — молча игнорирует любые запросы не от владельца."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id != OWNER_ID:
            return
        return await func(update, ctx)
    return wrapper


# ── Нативный Telegram Slideshow (Rich Message) ────────────────────────────────

async def _send_native_slideshow(
    app: Application,
    session: AlbumSession,
) -> int:
    """Отправляет нативный Telegram Slideshow через метод sendRichMessage."""
    total_candidates = len(session.candidates)
    total_pages = max(1, (total_candidates + PAGE_SIZE - 1) // PAGE_SIZE)
    current_page = session.page % total_pages
    start_idx = current_page * PAGE_SIZE
    end_idx = min(start_idx + PAGE_SIZE, total_candidates)
    display = session.candidates[start_idx:end_idx]

    slideshow_blocks = []
    for i, c in enumerate(display):
        global_idx = start_idx + i
        caption_parts = [f"#{global_idx + 1} · [{c.source}]"]
        if c.date:
            caption_parts.append(f"({c.date})")
        if c.tracks:
            caption_parts.append(f"· {c.tracks} треков")
        slide_caption = " ".join(caption_parts)

        slideshow_blocks.append({
            "type": "photo",
            "photo": {
                "type": "photo",
                "media": c.small_url,
            },
            "caption": {
                "text": slide_caption,
            },
        })

    # Кнопки с номерами и названиями источников (по 2 в ряд)
    buttons = []
    for i, c in enumerate(display):
        global_idx = start_idx + i
        buttons.append(
            InlineKeyboardButton(f"{global_idx + 1}. {c.source}", callback_data=f"cover:pick:{global_idx}")
        )

    keyboard = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]

    if total_pages > 1:
        keyboard.append([
            InlineKeyboardButton(
                f"🔄 Другие варианты ({current_page + 1}/{total_pages})",
                callback_data="cover:next_page",
            )
        ])

    skip_text = "⏭️ Оставить текущую" if session.is_recover else "⏭️ Без обложки"
    keyboard.append([
        InlineKeyboardButton(skip_text, callback_data="cover:skip"),
        InlineKeyboardButton("❌ Отмена", callback_data="cover:cancel"),
    ])

    page_info = f" (варианты {current_page + 1}/{total_pages})" if total_pages > 1 else ""
    header_text = (
        f"🖼️ Смена обложки: {session.artist} — {session.album}{page_info}\nСвайпай обложки вправо/влево и нажми кнопку:"
        if session.is_recover
        else f"📥 Новый релиз: {session.artist} — {session.album} ({session.n_tracks} треков){page_info}\nСвайпай обложки вправо/влево и нажми кнопку:"
    )

    payload = {
        "chat_id": OWNER_ID,
        "rich_message": {
            "blocks": [
                {
                    "type": "paragraph",
                    "text": header_text,
                },
                {
                    "type": "slideshow",
                    "blocks": slideshow_blocks,
                },
            ]
        },
        "reply_markup": {
            "inline_keyboard": [
                [{"text": b.text, "callback_data": b.callback_data} for b in row]
                for row in keyboard
            ]
        },
    }

    res = await app.bot._do_post("sendRichMessage", payload)
    return res.get("message_id")


# ── Основной flow: обнаружение релиза ─────────────────────────────────────────

async def _on_new_release(folder: Path, app: Application, is_recover: bool = False) -> None:
    """Запускает поиск обложек и открывает нативный слайдшоу."""
    artist, album = album_info(folder)
    n_tracks = count_flacs(folder)

    log.info("Релиз для обработки: %s — %s (is_recover=%s)", artist, album, is_recover)

    header = "🖼️ *Смена обложки в Library*" if is_recover else "📥 *Новый релиз в Inbox*"
    status_msg = await app.bot.send_message(
        chat_id=OWNER_ID,
        text=(
            f"{header}\n\n"
            f"🎵 {artist} — {album}\n"
            f"📂 `{folder.name}` ({n_tracks} треков)\n\n"
            f"🔍 Ищу обложки на стримингах…"
        ),
        parse_mode="Markdown",
    )

    loop = asyncio.get_running_loop()
    candidates: list[CoverCandidate] = await loop.run_in_executor(
        None, lambda: search_covers(artist, album) if (artist and album) else []
    )

    session = AlbumSession(
        state=SessionState.COVER_PENDING,
        folder=folder,
        artist=artist,
        album=album,
        n_tracks=n_tracks,
        candidates=candidates,
        is_recover=is_recover,
        page=0,
    )
    _sessions[OWNER_ID] = session

    # Удаляем временное сообщение "Ищу обложки…", чтобы не засорять чат
    try:
        await app.bot.delete_message(chat_id=OWNER_ID, message_id=status_msg.message_id)
    except Exception:
        pass

    if candidates:
        try:
            msg_id = await _send_native_slideshow(app, session)
            session.picker_message_id = msg_id
            return
        except Exception as e:
            log.warning("Не удалось отправить rich message slideshow: %s. Падаем на кнопки.", e)

    # Если обложек не найдено или произошла ошибка
    keyboard = [
        [InlineKeyboardButton("⏭️ Продолжить без обложки", callback_data="cover:skip")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cover:cancel")],
    ]
    no_covers_msg = await app.bot.send_message(
        chat_id=OWNER_ID,
        text=f"😕 Обложки для *{artist} — {album}* не найдены на стримингах.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    session.picker_message_id = no_covers_msg.message_id


# ── Обработчик выбора обложки ─────────────────────────────────────────────────

async def on_cover_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка выбора кнопки под слайдшоу."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != OWNER_ID:
        return

    session = _sessions.get(OWNER_ID)
    if not session or session.state != SessionState.COVER_PENDING:
        await ctx.bot.send_message(chat_id=OWNER_ID, text="⚠️ Сессия устарела. Запусти поиск заново.")
        return

    _, action, *args = query.data.split(":")

    # Удаляем сообщение со слайдшоу сразу при клике, чтобы чат оставался чистым
    if session.picker_message_id:
        try:
            await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=session.picker_message_id)
        except Exception:
            pass

    # 1. Переключение на следующую пачку обложек (Round-Robin)
    if action == "next_page":
        session.page += 1
        try:
            msg_id = await _send_native_slideshow(ctx.application, session)
            session.picker_message_id = msg_id
        except Exception as e:
            log.error("Ошибка при показе следующей страницы обложек: %s", e)
            await ctx.bot.send_message(
                chat_id=OWNER_ID,
                text=f"⚠️ Ошибка при переключении вариантов: `{e}`",
                parse_mode="Markdown",
            )
        return

    # 2. Отмена
    if action == "cancel":
        session.state = SessionState.DONE
        msg = "Обработка отменена." if not session.is_recover else "Смена обложки отменена."
        await ctx.bot.send_message(chat_id=OWNER_ID, text=f"❌ {msg}")
        return

    # 3. Пропуск обложки
    if action == "skip":
        if session.is_recover:
            session.state = SessionState.DONE
            await ctx.bot.send_message(chat_id=OWNER_ID, text="Обложка оставлена без изменений.")
            return

        session.chosen_cover = None
        session.state = SessionState.PROCESSING
        status_msg = await ctx.bot.send_message(chat_id=OWNER_ID, text="⚙️ Обрабатываю релиз без обложки…")
        await _execute_pipeline_and_report(status_msg, ctx, session)
        return

    # 4. Выбор обложки
    if action == "pick":
        idx = int(args[0])
        if not (0 <= idx < len(session.candidates)):
            await ctx.bot.send_message(chat_id=OWNER_ID, text="❌ Ошибка: неверный номер обложки.")
            return

        chosen = session.candidates[idx]
        session.chosen_cover = chosen
        session.state = SessionState.PROCESSING

        # Режим /cover (замена обложки в Library):
        if session.is_recover:
            status_msg = await ctx.bot.send_message(
                chat_id=OWNER_ID,
                text=f"⏳ Скачиваю обложку #{idx + 1} [{chosen.source}] и вшиваю в треки…"
            )
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None, lambda: update_album_cover(session.folder, chosen)
            )
            session.state = SessionState.DONE
            final_text = (
                f"✅ *Обложка успешно обновлена!*\n\n"
                f"🎵 {session.artist} — {session.album}\n"
                f"📂 `{session.folder.name}`\n"
                f"{res}"
            )
            await ctx.bot.edit_message_text(
                chat_id=OWNER_ID,
                message_id=status_msg.message_id,
                text=final_text,
                parse_mode="Markdown",
            )
            return

        # Обычный режим (Inbox -> Staging -> Library):
        status_msg = await ctx.bot.send_message(
            chat_id=OWNER_ID,
            text=f"⏳ Выбрана обложка #{idx + 1} [{chosen.source}]\n⚙️ Обрабатываю релиз…"
        )
        await _execute_pipeline_and_report(status_msg, ctx, session)


async def _execute_pipeline_and_report(status_msg, ctx: ContextTypes.DEFAULT_TYPE, session: AlbumSession) -> None:
    """Выполняет пайплайн и заменяет статусное сообщение на финальный аккуратный отчёт."""
    loop = asyncio.get_running_loop()
    try:
        report_lines, library_folder = await loop.run_in_executor(
            None, lambda: _run_full_pipeline(session)
        )
    except Exception as e:
        log.exception("Pipeline error for %s", session.folder.name)
        session.state = SessionState.DONE
        error_text = f"❌ Ошибка обработки: `{e}`"
        try:
            await ctx.bot.edit_message_text(
                chat_id=OWNER_ID,
                message_id=status_msg.message_id,
                text=error_text,
                parse_mode="Markdown",
            )
        except Exception:
            await ctx.bot.send_message(chat_id=OWNER_ID, text=error_text, parse_mode="Markdown")
        return

    session.state = SessionState.DONE
    report = "\n".join(report_lines)
    final_text = f"✅ *Альбом добавлен в библиотеку!*\n\n{report}"

    keyboard = [
        [InlineKeyboardButton("📝 Настроить тексты", callback_data=f"lyrics:album:{library_folder.name}")],
    ]

    try:
        await ctx.bot.edit_message_text(
            chat_id=OWNER_ID,
            message_id=status_msg.message_id,
            text=final_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        await ctx.bot.send_message(
            chat_id=OWNER_ID,
            text=final_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


def _run_full_pipeline(session: AlbumSession) -> tuple[list[str], Path]:
    """Синхронный полный pipeline: Inbox → Staging → обработка → Library."""
    report: list[str] = []

    # 1. Копируем в Staging
    staging_folder = copy_to_staging(session.folder, STAGING)
    session.staging_folder = staging_folder

    # 2. Скачиваем обложку (если выбрана)
    if session.chosen_cover:
        cover_line = apply_cover(staging_folder, session.chosen_cover)
        report.append(cover_line)

    # 3. Полный pipeline: теги → переименование → тексты
    pipeline_lines, staging_folder = apply_pipeline(
        staging_folder,
        db_path=DB_PATH,
        aliases_path=ALIASES_PATH,
    )
    report.extend(pipeline_lines)
    session.staging_folder = staging_folder

    # 4. Переносим в Library
    library_folder = move_to_library(staging_folder, LIBRARY)
    report.append(f"📦 Перенесено в: `{library_folder.name}`")

    # 5. Чистим Inbox
    cleanup_inbox(session.folder)
    report.append("🗑️ Inbox очищен")

    return report, library_folder


# ── Команды ───────────────────────────────────────────────────────────────────

@_owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎵 *NaviSan бот запущен* ✅\n\n"
        "Бот автоматически следит за папкой Inbox.\n"
        "Когда slskd докачает новый релиз — я пришлю нативный слайдшоу обложек "
        "(свайпай прямо в Telegram!) и добавлю альбом без лишнего мусора в чат.\n\n"
        "*Команды:*\n"
        "• `/scan` — сканировать Inbox и выбрать релиз вручную\n"
        "• `/cover [поиск]` — сменить обложку для альбома в Library\n"
        "• `/lyrics [поиск]` — превью и настройка текстов песен (.lrc)\n"
        "• `/process <папка>` — принудительно обработать релиз из Inbox\n"
        "• `/status` — статус текущей сессии\n"
        "• `/cancel` — отменить текущий ввод текста",
        parse_mode="Markdown",
    )


@_owner_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(OWNER_ID)
    if not session:
        await update.message.reply_text("Нет активных сессий. Жду новых релизов в Inbox.")
        return

    state_emoji = {
        SessionState.COVER_PENDING: "🖼️ Ожидание выбора обложки",
        SessionState.PROCESSING: "⚙️ Обработка",
        SessionState.DONE: "✅ Завершено",
    }
    cover_info = session.chosen_cover.source if session.chosen_cover else "не выбрана"

    await update.message.reply_text(
        f"📂 Папка: `{session.folder.name}`\n"
        f"🎵 {session.artist} — {session.album}\n"
        f"📊 Статус: {state_emoji.get(session.state, '❓')}\n"
        f"🖼️ Обложка: {cover_info}\n"
        f"🔢 Кандидатов: {len(session.candidates)}",
        parse_mode="Markdown",
    )


@_owner_only
async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Принудительное сканирование Inbox — для папок, которые уже лежали в папке."""
    if not INBOX.exists():
        await update.message.reply_text("❌ Папка Inbox не найдена.", parse_mode="Markdown")
        return

    folders = sorted([
        d for d in INBOX.iterdir()
        if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.iterdir() if f.is_file())
    ])

    if not folders:
        await update.message.reply_text("📭 Inbox пуст — нет папок с FLAC-файлами.")
        return

    lines = [f"📥 *Папки в Inbox ({len(folders)}):*\n"]
    buttons = []
    for i, d in enumerate(folders):
        artist, album = album_info(d)
        n = count_flacs(d)
        display = f"{artist} — {album}" if album else d.name
        lines.append(f"{i + 1}. `{d.name}` — {display} ({n} треков)")
        buttons.append(InlineKeyboardButton(str(i + 1), callback_data=f"scan:{i}"))

    ctx.bot_data["scan_folders"] = folders
    keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_scan_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик выбора папки из /scan."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != OWNER_ID:
        return

    folders = ctx.bot_data.get("scan_folders", [])
    _, payload = query.data.split(":", 1)
    idx = int(payload)

    if idx >= len(folders):
        await query.edit_message_text("❌ Некорректный номер.")
        return

    folder = folders[idx]
    try:
        await query.delete_message()
    except Exception:
        pass

    await _on_new_release(folder, ctx.application)


@_owner_only
async def cmd_cover(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Смена обложки для уже существующего альбома в Library."""
    if not LIBRARY.exists():
        await update.message.reply_text("❌ Папка Library не найдена.", parse_mode="Markdown")
        return

    query_str = " ".join(ctx.args).strip() if ctx.args else ""

    all_albums = sorted([
        d for d in LIBRARY.iterdir()
        if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.iterdir() if f.is_file())
    ])

    if not all_albums:
        await update.message.reply_text("📭 В Library пока нет альбомов с FLAC-файлами.")
        return

    if query_str:
        matches = [d for d in all_albums if query_str.lower() in d.name.lower()]
    else:
        matches = all_albums[-10:]

    if not matches:
        await update.message.reply_text(f"❌ Ничего не найдено по запросу: `{query_str}`", parse_mode="Markdown")
        return

    # Если ровно одно совпадение — сразу открываем слайдшоу!
    if len(matches) == 1 and query_str:
        await _on_new_release(matches[0], ctx.application, is_recover=True)
        return

    # Если несколько — даём список с кнопками
    lines = [f"🖼️ *Выбери альбом для смены обложки ({len(matches)}):*\n"]
    buttons = []
    for i, d in enumerate(matches):
        artist, album = album_info(d)
        display = f"{artist} — {album}" if album else d.name
        lines.append(f"{i + 1}. `{d.name}` — {display}")
        buttons.append(InlineKeyboardButton(str(i + 1), callback_data=f"recover:{i}"))

    ctx.bot_data["recover_folders"] = matches
    keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_recover_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбор альбома для /cover."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != OWNER_ID:
        return

    folders = ctx.bot_data.get("recover_folders", [])
    _, payload = query.data.split(":", 1)
    idx = int(payload)

    if idx >= len(folders):
        await query.edit_message_text("❌ Некорректный номер.")
        return

    folder = folders[idx]
    try:
        await query.delete_message()
    except Exception:
        pass

    await _on_new_release(folder, ctx.application, is_recover=True)


@_owner_only
async def cmd_process(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручной запуск обработки для указанной папки из Inbox."""
    if not ctx.args:
        await update.message.reply_text(
            "Укажи имя папки в Inbox:\n`/process Имя Папки`\nИли выбери папку через `/scan`",
            parse_mode="Markdown",
        )
        return

    target_str = " ".join(ctx.args).strip()
    folder = Path(target_str)

    # Если передан относительный путь или только имя папки в Inbox
    if not folder.is_absolute() or not folder.exists():
        inbox_candidate = INBOX / target_str
        if inbox_candidate.exists():
            folder = inbox_candidate

    if not folder.exists() or not folder.is_dir():
        await update.message.reply_text(f"❌ Папка не найдена: `{Path(target_str).name}`", parse_mode="Markdown")
        return

    await _on_new_release(folder, ctx.application)


# ── Настройка текстов песен (Lyrics UI) ───────────────────────────────────────

def _render_album_lyrics_tracklist(album_dir: Path, tracks: list[Path]) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Формирует список треков альбома со статусами текстов и кнопками выбора."""
    artist, album = album_info(album_dir)
    title_str = f"{artist} — {album}" if album else album_dir.name

    lines = [f"📝 *Тексты альбома:*\n🎵 *{title_str}*\n"]
    buttons = []

    for i, flac in enumerate(tracks):
        lrc = flac.with_suffix(".lrc")
        if lrc.exists():
            try:
                content = lrc.read_text(encoding="utf-8")
                icon = "✨" if has_timestamps(content) else "📄"
            except Exception:
                icon = "📄"
        else:
            icon = "❌"

        lines.append(f"{i + 1}. {icon} `{flac.name}`")
        buttons.append(InlineKeyboardButton(str(i + 1), callback_data=f"lyrics:tr:{i}"))

    lines.append("\n_Обозначения: ✨ — караоке, 📄 — обычный текст, ❌ — текста нет_\n")
    lines.append("Нажми номер трека, чтобы просмотреть или сменить текст:")

    keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="lyrics:close")])

    return "\n".join(lines), keyboard


def _render_track_lyrics_card(track_path: Path, track_idx: int) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Формирует карточку трека с превью текста и кнопками смены источника."""
    try:
        audio = FLAC(str(track_path))
        artist = audio.get("artist", audio.get("albumartist", [""]))[0]
        title = audio.get("title", [""])[0]
    except Exception:
        artist, title = "", ""

    if not title:
        title = track_path.stem

    lrc_path = track_path.with_suffix(".lrc")
    text_content = ""
    if lrc_path.exists():
        try:
            text_content = lrc_path.read_text(encoding="utf-8").strip()
        except Exception:
            text_content = ""

    if text_content:
        is_sync = has_timestamps(text_content)
        status = "✨ *Караоке (Синхронизированный)*" if is_sync else "📄 *Обычный текст (Plain)*"
        preview = preview_lyrics(text_content, max_lines=6)
    else:
        status = "❌ *Текст отсутствует*"
        preview = "—"

    header_artist = f"{artist} — " if artist else ""
    text = (
        f"🎵 *{header_artist}{title}*\n"
        f"📂 `{track_path.name}`\n"
        f"📊 Статус: {status}\n\n"
        f"📝 *Превью текста:*\n```\n{preview}\n```"
    )

    keyboard = [
        [
            InlineKeyboardButton("🔍 LRCLIB", callback_data=f"lyrics:src:lrclib:{track_idx}"),
            InlineKeyboardButton("🔍 Genius", callback_data=f"lyrics:src:genius:{track_idx}"),
            InlineKeyboardButton("🔍 Яндекс", callback_data=f"lyrics:src:yandex:{track_idx}"),
        ],
        [
            InlineKeyboardButton("✏️ Прислать свой текст / .lrc", callback_data=f"lyrics:man:{track_idx}"),
        ],
        [
            InlineKeyboardButton("⬅️ К списку треков", callback_data="lyrics:back"),
            InlineKeyboardButton("❌ Закрыть", callback_data="lyrics:close"),
        ],
    ]
    return text, keyboard


@_owner_only
async def cmd_lyrics(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /lyrics [запрос] — просмотр и настройка текстов песен в Library."""
    if not LIBRARY.exists():
        await update.message.reply_text("❌ Папка Library не найдена.", parse_mode="Markdown")
        return

    query_str = " ".join(ctx.args).strip() if ctx.args else ""

    all_albums = sorted([
        d for d in LIBRARY.iterdir()
        if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.iterdir() if f.is_file())
    ])

    if not all_albums:
        await update.message.reply_text("📭 В Library пока нет альбомов с FLAC-файлами.")
        return

    if query_str:
        matches = [d for d in all_albums if query_str.lower() in d.name.lower()]
    else:
        matches = all_albums[-10:]

    if not matches:
        await update.message.reply_text(f"❌ Ничего не найдено по запросу: `{query_str}`", parse_mode="Markdown")
        return

    # Если ровно один альбом совпал — открываем его список треков!
    if len(matches) == 1:
        album_dir = matches[0]
        tracks = sorted(album_dir.rglob("*.flac"))
        ctx.bot_data["lyrics_tracks"] = tracks
        ctx.bot_data["lyrics_album_path"] = album_dir
        text, keyboard = _render_album_lyrics_tracklist(album_dir, tracks)
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # Если несколько альбомов — выводим список с кнопками
    lines = [f"📝 *Выбери альбом для настройки текстов ({len(matches)}):*\n"]
    buttons = []
    for i, d in enumerate(matches):
        artist, album = album_info(d)
        display = f"{artist} — {album}" if album else d.name
        lines.append(f"{i + 1}. `{d.name}` — {display}")
        buttons.append(InlineKeyboardButton(str(i + 1), callback_data=f"lyrics:alb:{i}"))

    ctx.bot_data["lyrics_albums"] = matches
    keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="lyrics:close")])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@_owner_only
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбрасывает ожидание ручного ввода текста."""
    waiting_key = f"lyrics_waiting_{OWNER_ID}"
    if waiting_key in ctx.bot_data:
        del ctx.bot_data[waiting_key]
        await update.message.reply_text("Ввод текста отменён.")
    else:
        await update.message.reply_text("Нет активных действий для отмены.")


async def on_lyrics_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик кнопок управления текстами."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != OWNER_ID:
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "close":
        try:
            await query.delete_message()
        except Exception:
            pass
        return

    if action == "album":
        album_name = parts[2]
        album_dir = LIBRARY / album_name
        if not album_dir.exists():
            matches = [d for d in LIBRARY.iterdir() if d.is_dir() and album_name.lower() in d.name.lower()]
            if matches:
                album_dir = matches[0]

        if not album_dir.exists():
            await query.edit_message_text(f"❌ Альбом `{album_name}` не найден.")
            return

        tracks = sorted(album_dir.rglob("*.flac"))
        if not tracks:
            await query.edit_message_text("❌ В альбоме нет FLAC-треков.")
            return

        ctx.bot_data["lyrics_tracks"] = tracks
        ctx.bot_data["lyrics_album_path"] = album_dir

        text, keyboard = _render_album_lyrics_tracklist(album_dir, tracks)
        await ctx.bot.send_message(
            chat_id=OWNER_ID,
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if action == "alb":
        idx = int(parts[2])
        albums = ctx.bot_data.get("lyrics_albums", [])
        if not (0 <= idx < len(albums)):
            return
        album_dir = albums[idx]
        tracks = sorted(album_dir.rglob("*.flac"))
        ctx.bot_data["lyrics_tracks"] = tracks
        ctx.bot_data["lyrics_album_path"] = album_dir

        text, keyboard = _render_album_lyrics_tracklist(album_dir, tracks)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "back":
        album_dir = ctx.bot_data.get("lyrics_album_path")
        tracks = ctx.bot_data.get("lyrics_tracks")
        if not album_dir or not tracks:
            await query.edit_message_text("⚠️ Сессия устарела. Используй /lyrics.")
            return

        text, keyboard = _render_album_lyrics_tracklist(album_dir, tracks)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "tr":
        idx = int(parts[2])
        tracks = ctx.bot_data.get("lyrics_tracks", [])
        if not (0 <= idx < len(tracks)):
            return

        track_path = tracks[idx]
        text, keyboard = _render_track_lyrics_card(track_path, idx)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "src":
        source = parts[2]
        idx = int(parts[3])
        tracks = ctx.bot_data.get("lyrics_tracks", [])
        if not (0 <= idx < len(tracks)):
            return

        track_path = tracks[idx]
        try:
            audio = FLAC(str(track_path))
            artist = audio.get("artist", audio.get("albumartist", [""]))[0]
            title = audio.get("title", [""])[0]
            album = audio.get("album", [""])[0]
            duration = int(audio.info.length) if hasattr(audio.info, "length") else 0
        except Exception:
            artist, title, album, duration = "", track_path.stem, "", 0

        source_names = {"lrclib": "LRCLIB", "genius": "Genius", "yandex": "Яндекс Музыке"}
        src_name = source_names.get(source, source)

        await query.answer(f"🔍 Ищу на {src_name}…")

        lm = LyricManager()
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            None, lambda: lm.find_lyrics(artist, title, album, duration, preferred_source=source)
        )

        if not res or not res.text:
            await query.answer(f"😕 На {src_name} текст не найден", show_alert=True)
            return

        # Сохраняем найденный текст
        lrc_path = track_path.with_suffix(".lrc")
        lrc_path.write_text(res.text, encoding="utf-8")

        await query.answer(f"✅ Текст успешно обновлён из {src_name}!")
        text, keyboard = _render_track_lyrics_card(track_path, idx)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "man":
        idx = int(parts[2])
        tracks = ctx.bot_data.get("lyrics_tracks", [])
        if not (0 <= idx < len(tracks)):
            return

        track_path = tracks[idx]
        ctx.bot_data[f"lyrics_waiting_{OWNER_ID}"] = str(track_path)

        try:
            audio = FLAC(str(track_path))
            artist = audio.get("artist", [""])[0]
            title = audio.get("title", [""])[0]
        except Exception:
            artist, title = "", track_path.stem

        name_str = f"{artist} — {title}" if artist else title
        prompt = (
            f"✍️ *Ручной ввод текста для трека:*\n🎵 *{name_str}*\n\n"
            f"Отправь мне текст песни сообщением в ответ или прикрепи файл `.lrc` документом.\n"
            f"Для отмены отправь команду /cancel."
        )
        await ctx.bot.send_message(chat_id=OWNER_ID, text=prompt, parse_mode="Markdown")
        return


async def on_owner_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает присланные владельцем файлы .lrc или текст песен."""
    if update.effective_user.id != OWNER_ID:
        return

    waiting_key = f"lyrics_waiting_{OWNER_ID}"
    track_path_str = ctx.bot_data.get(waiting_key)
    if not track_path_str:
        return

    track_path = Path(track_path_str)
    text_to_save = ""

    # Если отправлен документ (.lrc / .txt)
    if update.message.document:
        doc = update.message.document
        file_obj = await doc.get_file()
        file_bytes = await file_obj.download_as_bytearray()
        text_to_save = file_bytes.decode("utf-8", errors="replace").strip()

    # Если отправлен обычный текст
    elif update.message.text:
        text_to_save = update.message.text.strip()

    if not text_to_save:
        await update.message.reply_text("❌ Пустой текст. Отправка отменена.")
        del ctx.bot_data[waiting_key]
        return

    lrc_path = track_path.with_suffix(".lrc")
    lrc_path.write_text(text_to_save, encoding="utf-8")
    del ctx.bot_data[waiting_key]

    is_sync = has_timestamps(text_to_save)
    status = "караоке ✨" if is_sync else "обычный текст 📄"

    await update.message.reply_text(
        f"✅ *Текст песни успешно сохранён!*\n"
        f"📂 `{lrc_path.name}` ({status})\n\n"
        f"📝 *Превью:*\n```\n{preview_lyrics(text_to_save, 6)}\n```",
        parse_mode="Markdown",
    )


# ── Запуск ────────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    """Запускает Inbox Watcher после инициализации бота."""
    async def on_release(folder: Path) -> None:
        await _on_new_release(folder, app)

    asyncio.create_task(watch_inbox(INBOX, on_release))
    log.info("Inbox watcher задача создана")

    try:
        await app.bot.send_message(
            chat_id=OWNER_ID,
            text="🟢 NaviSan бот запущен. Слежу за Inbox.",
        )
    except Exception:
        log.warning("Не удалось отправить приветственное сообщение")


def main() -> None:
    request = HTTPXRequest(connect_timeout=20.0, read_timeout=20.0)
    app = Application.builder().token(TOKEN).request(request).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("cover", cmd_cover))
    app.add_handler(CommandHandler("lyrics", cmd_lyrics))
    app.add_handler(CommandHandler("process", cmd_process))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Callback-кнопки
    app.add_handler(CallbackQueryHandler(on_cover_callback, pattern=r"^cover:"))
    app.add_handler(CallbackQueryHandler(on_scan_callback, pattern=r"^scan:"))
    app.add_handler(CallbackQueryHandler(on_recover_callback, pattern=r"^recover:"))
    app.add_handler(CallbackQueryHandler(on_lyrics_callback, pattern=r"^lyrics:"))

    # Сообщения владельца (текст или документы .lrc для ручного ввода текста песни)
    app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, on_owner_message))

    app.post_init = post_init

    log.info("NaviSan бот запускается (owner=%d, inbox=%s, library=%s)",
             OWNER_ID, INBOX, LIBRARY)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
