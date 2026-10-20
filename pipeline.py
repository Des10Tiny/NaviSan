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

from covers import CoverCandidate, download_cover, search_covers
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


# ── Управление артистами ─────────────────────────────────────────────────────

def get_album_artists_info(folder: Path) -> dict:
    """Возвращает информацию об артистах релиза и его треков."""
    flac_files = sorted(folder.rglob("*.flac"))
    album_artist_val = ""
    album_title = ""
    tracks_info = []

    for f in flac_files:
        try:
            audio = FLAC(str(f))
            if not album_artist_val:
                album_artist_val = ", ".join(audio.get("albumartist", []))
            if not album_title:
                album_title = audio.get("album", [""])[0]

            track_num = audio.get("tracknumber", [""])[0].split("/")[0]
            title = audio.get("title", [f.stem])[0]
            artists = audio.get("artist", [])

            tracks_info.append({
                "path": f,
                "track_num": track_num,
                "title": title,
                "artists": artists,
            })
        except Exception:
            continue

    return {
        "folder": folder,
        "album": album_title or folder.name,
        "album_artist": album_artist_val or "—",
        "tracks": tracks_info,
    }


def get_album_artist_suggestions(folder: Path) -> list[str]:
    """Формирует список рекомендуемых вариантов ALBUMARTIST на основе треков альбома."""
    info = get_album_artists_info(folder)
    tracks = info.get("tracks", [])
    if not tracks:
        return []

    track_artist_sets = [set(t["artists"]) for t in tracks if t["artists"]]
    all_unique: list[str] = []
    for t in tracks:
        for a in t["artists"]:
            if a not in all_unique:
                all_unique.append(a)

    suggestions: list[str] = []

    # 1. Все уникальные исполнители треков (если их до 4)
    if all_unique and len(all_unique) <= 4:
        all_str = ", ".join(all_unique)
        if all_str not in suggestions:
            suggestions.append(all_str)

    # 2. Общие артисты для всех треков (если отличаются от всех уникальных)
    if track_artist_sets:
        common = set.intersection(*track_artist_sets)
        if common:
            common_ordered = [a for a in all_unique if a in common]
            common_str = ", ".join(common_ordered)
            if common_str and common_str not in suggestions:
                suggestions.append(common_str)

    # 3. Текущий ALBUMARTIST (если уже есть и не в списке)
    current_aa = info.get("album_artist", "")
    if current_aa and current_aa != "—" and current_aa not in suggestions:
        suggestions.append(current_aa)

    # 4. Если на треках больше 2 разных артистов — предложить "Various Artists"
    if len(tracks) > 1 and len(all_unique) > 2 and "Various Artists" not in suggestions:
        suggestions.append("Various Artists")

    return suggestions


def add_track_artist(track_path: Path, new_artist: str) -> list[str]:
    """Добавляет одного или нескольких артистов в тег ARTIST трека."""
    new_artist = new_artist.strip()
    if not new_artist or not track_path.exists():
        return []

    sanitizer = TagSanitizer()
    to_add = sanitizer.process_artists(new_artist)
    if not to_add:
        to_add = [new_artist]

    audio = FLAC(str(track_path))
    artists = list(audio.get("artist", []))
    for art in to_add:
        if art not in artists:
            artists.append(art)
    audio["artist"] = artists
    audio.save()
    return audio.get("artist", [])


def remove_track_artist(track_path: Path, artist_to_remove: str) -> list[str]:
    """Удаляет артиста из тега ARTIST трека."""
    if not track_path.exists():
        return []

    audio = FLAC(str(track_path))
    artists = [a for a in audio.get("artist", []) if a.strip().lower() != artist_to_remove.strip().lower()]
    audio["artist"] = artists
    audio.save()
    return audio.get("artist", [])


def update_album_artist(folder: Path, new_album_artist: str) -> tuple[Path, str]:
    """Обновляет тег ALBUMARTIST у всех треков альбома и переименовывает папку."""
    flac_files = sorted(folder.rglob("*.flac"))
    if not flac_files:
        return folder, "В папке нет FLAC-файлов."

    raw = new_album_artist.strip()
    if raw in ("-", "none", "del", "delete", "clear", ""):
        aa_list: list[str] = []
    else:
        sanitizer = TagSanitizer()
        aa_list = sanitizer.process_artists(raw)
        if not aa_list and raw:
            aa_list = [raw]

    for f in flac_files:
        try:
            audio = FLAC(str(f))
            if aa_list:
                audio["albumartist"] = aa_list
            else:
                if "albumartist" in audio:
                    del audio["albumartist"]
            audio.save()
        except Exception as e:
            log.warning("Не удалось обновить ALBUMARTIST в %s: %s", f.name, e)

    renamer = FileSystemManager()
    new_folder_str = renamer.rename_album_folder(str(folder))
    status_str = f"Альбом-артист обновлён на: {', '.join(aa_list)}" if aa_list else "Тег ALBUMARTIST удалён"
    return Path(new_folder_str), status_str


# ── Пакетная идеализация библиотеки ──────────────────────────────────────────

def album_has_cover(album_dir: Path) -> bool:
    """Проверяет наличие обложки в папке альбома или встроенной в FLAC."""
    for ext in ("cover.jpg", "cover.png", "folder.jpg", "folder.png"):
        if (album_dir / ext).exists():
            return True
    for f in sorted(album_dir.rglob("*.flac")):
        try:
            audio = FLAC(str(f))
            if audio.pictures and len(audio.pictures[0].data) > 30_000:
                return True
        except Exception:
            continue
    return False


def upgrade_album(
    album_dir: Path,
    aliases_path: str | Path = "aliases.json",
    db_path: str | Path = "navidrome.db",
    force_cover: bool = False,
) -> dict:
    """Выполняет полную стандартизацию («идеализацию») альбома на месте.

    1. Проверяет обложку: если её нет (или force_cover=True), ищет и вшивает.
    2. Очищает и нормализует теги (FLACSurgeon + TagSanitizer).
    3. Ищет и сохраняет недостающие караоке-тексты (.lrc).
    4. Приводит имена треков к '01. Title.flac' и папки к 'Artist - Album'.
    """
    flac_files = sorted(album_dir.rglob("*.flac"))
    if not flac_files:
        return {"error": "no_flacs", "folder": album_dir.name}

    artist, album = album_info(album_dir)
    cover_added = False
    lyrics_added = 0

    # 1. Обложка: проверяем и сохраняем существующую, ищем только если отсутствует
    if force_cover or not album_has_cover(album_dir):
        if artist and album:
            try:
                candidates = search_covers(artist, album)
                if candidates:
                    chosen = candidates[0]
                    update_album_cover(album_dir, chosen)
                    cover_added = True
            except Exception as e:
                log.warning("Не удалось найти обложку для %s — %s: %s", artist, album, e)

    # 2. Санитизация тегов, переименование треков и подгрузка .lrc
    sanitizer = TagSanitizer(aliases_path=aliases_path, db_path=db_path)
    surgeon = FLACSurgeon(sanitizer=sanitizer)
    renamer = FileSystemManager()
    lyricist = LyricManager()

    flac_files = sorted(album_dir.rglob("*.flac"))
    for flac in flac_files:
        try:
            surgeon.heal_file(str(flac))
            new_flac = renamer.rename_file(str(flac))
            lrc_path = Path(new_flac).with_suffix(".lrc")
            if not lrc_path.exists():
                res_status = lyricist.process_file(new_flac)
                if "LRCLIB" in res_status or "Genius" in res_status:
                    lyrics_added += 1
        except Exception as e:
            log.warning("Ошибка обработки трека %s: %s", flac.name, e)

    # 3. Переименование папки альбома
    new_folder_str = renamer.rename_album_folder(str(album_dir))
    new_folder = Path(new_folder_str)

    return {
        "old_folder": album_dir.name,
        "new_folder": new_folder.name,
        "folder_path": new_folder,
        "tracks_count": len(flac_files),
        "cover_added": cover_added,
        "lyrics_added": lyrics_added,
    }


def upgrade_library(
    library_dir: Path,
    aliases_path: str | Path = "aliases.json",
    db_path: str | Path = "navidrome.db",
    progress_callback: Callable[[int, int, str, dict], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> dict:
    """Выполняет пакетную стандартизацию («идеализацию») всех альбомов в Library."""
    all_albums = sorted([
        d for d in library_dir.iterdir()
        if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.rglob("*.flac") if f.is_file())
    ])

    total = len(all_albums)
    processed = 0
    total_covers = 0
    total_lyrics = 0
    results = []

    for i, album_folder in enumerate(all_albums, 1):
        if cancel_flag and cancel_flag():
            log.info("Идеализация библиотеки прервана пользователем")
            break

        res = upgrade_album(album_folder, aliases_path=aliases_path, db_path=db_path)
        processed += 1
        if res.get("cover_added"):
            total_covers += 1
        total_lyrics += res.get("lyrics_added", 0)
        results.append(res)

        if progress_callback:
            try:
                progress_callback(i, total, album_folder.name, res)
            except Exception as e:
                log.warning("Ошибка progress_callback: %s", e)

    return {
        "total": total,
        "processed": processed,
        "covers_added": total_covers,
        "lyrics_added": total_lyrics,
        "results": results,
    }
