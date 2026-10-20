"""Staging pipeline: Inbox → Staging → обработка → Library (ADR-0008, ADR-0011).

Модуль отвечает за безопасный перенос релиза через промежуточный каталог
Staging, применение всех шагов обработки (теги, обложка, тексты,
переименование) и атомарный перенос готового результата в Library.

Все функции — синхронные (CPU/IO-bound): бот вызывает их через
``run_in_executor``, чтобы не блокировать event loop.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from mutagen.flac import FLAC

from covers import CoverCandidate, download_cover
from sanitizer import TagSanitizer
from surgeon import FLACSurgeon
from renamer import FileSystemManager
from lyricist import LyricManager

log = logging.getLogger(__name__)


# ── Информация об альбоме ────────────────────────────────────────────────────

def album_info(folder: Path) -> tuple[str, str]:
    """Возвращает (artist, album) из первого читаемого FLAC в папке.

    artist берётся из albumartist (или artist), только первый артист
    (до первого '&'), чтобы поиск обложек был точнее.
    """
    for f in sorted(folder.rglob("*.flac")):
        try:
            audio = FLAC(str(f))
            artist = (
                audio.get("albumartist", audio.get("artist", [""]))[0]
                .split("&")[0]
                .strip()
            )
            album = audio.get("album", [""])[0]
            if artist or album:
                return artist, album
        except Exception:
            continue
    return folder.name, ""


def count_flacs(folder: Path) -> int:
    """Количество FLAC-файлов в папке (рекурсивно)."""
    return sum(1 for _ in folder.rglob("*.flac"))


# ── Staging: копирование ─────────────────────────────────────────────────────

def copy_to_staging(src: Path, staging_root: Path) -> Path:
    """Копирует папку релиза из Inbox в Staging.

    Если в Staging уже есть папка с таким именем — удаляет старую
    (это повторная обработка того же релиза).

    Returns:
        Путь к копии в Staging.
    """
    dest = staging_root / src.name
    if dest.exists():
        log.info("Staging: удаляю старую копию %s", dest.name)
        shutil.rmtree(dest)

    staging_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    log.info("Staging: скопировано %s → %s", src, dest)
    return dest


# ── Обработка (Apply) ────────────────────────────────────────────────────────

def apply_cover(
    folder: Path,
    cover: CoverCandidate,
) -> str:
    """Скачивает выбранную обложку и записывает дату в теги.

    Returns:
        Краткий отчёт (строка).
    """
    dest = folder / "cover.jpg"
    download_cover(cover.big_url, dest)

    date_note = ""
    if cover.date:
        _apply_date_to_flacs(folder, cover.date)
        date_note = f", дата {cover.date} → теги"

    return f"🖼️ Обложка [{cover.source}]{date_note}"


def update_album_cover(folder: Path, cover: CoverCandidate) -> str:
    """Заменяет обложку и обновляет дату в уже существующем альбоме в Library."""
    from mutagen.flac import Picture

    dest = folder / "cover.jpg"
    download_cover(cover.big_url, dest)
    with open(dest, "rb") as f:
        img_data = f.read()

    pic = Picture()
    pic.type = 3
    pic.mime = "image/jpeg"
    pic.desc = "Front Cover"
    pic.data = img_data

    flacs = sorted(folder.rglob("*.flac"))
    for f in flacs:
        try:
            audio = FLAC(str(f))
            audio.clear_pictures()
            audio.add_picture(pic)
            if cover.date:
                existing = audio.get("date", audio.get("year", [""]))[0]
                if not existing or _date_is_more_specific(cover.date, existing):
                    audio["date"] = [cover.date]
                    audio["year"] = [cover.date[:4]]
            audio.save()
        except Exception as e:
            log.warning("Не удалось обновить обложку в %s: %s", f.name, e)

    return f"🖼️ Обложка [{cover.source}] успешно вшита в {len(flacs)} треков"


def apply_pipeline(
    folder: Path,
    *,
    db_path: str,
    aliases_path: str,
) -> tuple[list[str], Path]:
    """Запускает полный pipeline обработки для всех FLAC в папке.

    Порядок шагов:
    1. surgeon.heal_file — чистка тегов, вшивание cover.jpg, синхронизация дат
    2. renamer.rename_file — переименование трека (01. Название.flac)
    3. lyricist.process_file — скачивание .lrc
    4. renamer.rename_album_folder — переименование папки (Artist - Album)

    Returns:
        Кортеж (список строк-отчёта, обновлённый путь к папке в Staging).
    """
    sanitizer = TagSanitizer(aliases_path=aliases_path, db_path=db_path)
    surgeon = FLACSurgeon(sanitizer=sanitizer)
    renamer = FileSystemManager()
    lyricist = LyricManager()

    report: list[str] = []

    flac_files = sorted(folder.rglob("*.flac"))
    if not flac_files:
        return ["⚠️ FLAC-файлы не найдены в staging"], folder

    for flac in flac_files:
        surgeon.heal_file(str(flac))
        new_path = renamer.rename_file(str(flac))
        lrc_status = lyricist.process_file(new_path)
        report.append(f"• `{Path(new_path).name}` — {lrc_status}")

    # Переименовываем папку альбома последней (пути выше уже отработали).
    new_folder = Path(renamer.rename_album_folder(str(folder)))
    report.append(f"📁 `{new_folder.name}`")

    return report, new_folder


# ── Перенос в Library ────────────────────────────────────────────────────────

def move_to_library(staging_folder: Path, library_root: Path) -> Path:
    """Атомарно переносит обработанную папку из Staging в Library.

    Если в Library уже есть папка с таким именем — добавляет суффикс,
    чтобы не перезаписывать существующий релиз.

    Returns:
        Путь к папке в Library.
    """
    dest = library_root / staging_folder.name

    # Защита от перезаписи существующего релиза.
    if dest.exists():
        counter = 2
        while dest.exists():
            dest = library_root / f"{staging_folder.name} ({counter})"
            counter += 1
        log.warning("Library: папка уже существует, сохраняю как %s", dest.name)

    library_root.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging_folder), str(dest))
    log.info("Library: перенесено %s → %s", staging_folder.name, dest)
    return dest


def cleanup_inbox(inbox_folder: Path) -> None:
    """Удаляет оригинальную папку из Inbox после успешного переноса в Library."""
    if inbox_folder.exists():
        shutil.rmtree(inbox_folder)
        log.info("Inbox: удалено %s", inbox_folder.name)


# ── Хелперы ──────────────────────────────────────────────────────────────────

def _date_is_more_specific(new_date: str, existing_date: str) -> bool:
    """True, если *new_date* содержит больше информации, чем *existing_date*.

    Критерий: длина строки. "2019-06-21" (10) > "2019" (4).
    """
    return len(new_date.strip()) > len(existing_date.strip())


def _apply_date_to_flacs(folder: Path, date_str: str) -> None:
    """Записывает дату в теги DATE/YEAR всех FLAC в папке (если новая специфичнее)."""
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
