"""Модуль расчёта и управления ReplayGain для FLAC-библиотеки.

Использует официальную утилиту metaflac (Xiph.Org) для быстрого неразрушающего
расчёта психоакустической громкости (EBU R128 / ReplayGain 2.0).
Записывает в теги каждого трека:
- REPLAYGAIN_TRACK_GAIN / REPLAYGAIN_TRACK_PEAK
- REPLAYGAIN_ALBUM_GAIN / REPLAYGAIN_ALBUM_PEAK
- REPLAYGAIN_REFERENCE_LOUDNESS
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from mutagen.flac import FLAC

log = logging.getLogger(__name__)


def find_metaflac() -> str | None:
    """Ищет исполняемый файл metaflac в системе."""
    # 1. Системный PATH
    found = shutil.which("metaflac")
    if found:
        return found

    # 2. Пользовательский ~/.local/bin
    user_bin = Path.home() / ".local/bin/metaflac"
    if user_bin.exists() and os.access(user_bin, os.X_OK):
        return str(user_bin)

    # 3. Стандартные пути Linux
    for p in ("/usr/bin/metaflac", "/usr/local/bin/metaflac", "/bin/metaflac"):
        if Path(p).exists():
            return p

    return None


def has_replaygain(track_path: str | Path) -> bool:
    """Проверяет наличие ReplayGain тегов в FLAC-файле."""
    try:
        audio = FLAC(str(track_path))
        gain = audio.get("replaygain_track_gain")
        return bool(gain and gain[0].strip())
    except Exception:
        return False


def album_has_replaygain(album_dir: str | Path) -> bool:
    """Проверяет, проставлен ли ReplayGain для всех треков альбома."""
    root = Path(album_dir)
    flacs = sorted(root.rglob("*.flac"))
    if not flacs:
        return False
    return all(has_replaygain(f) for f in flacs)


def apply_replaygain(album_dir: str | Path) -> tuple[bool, str]:
    """Рассчитывает и записывает ReplayGain (потрековый и альбомный) для всех FLAC в папке.

    Запускает metaflac --add-replay-gain на все треки альбома разом,
    чтобы рассчитать как индивидуальный Track Gain каждого трека,
    так и единый Album Gain для всего релиза.
    Если треки имеют разные аудиоформаты (sample rate / bit depth) или среди них есть
    повреждённый файл, выполняет адаптивный расчёт по совместимым группам и потреково.

    Returns:
        (успех: bool, сообщение: str)
    """
    root = Path(album_dir)
    flacs = sorted(root.rglob("*.flac"))
    if not flacs:
        return False, "В папке нет FLAC-файлов"

    metaflac_bin = find_metaflac()
    if not metaflac_bin:
        err = "Утилита metaflac не найдена в системе"
        log.warning(err)
        return False, err

    # 1. Попытка единого альбомного расчёта для всех треков разом
    cmd = [metaflac_bin, "--add-replay-gain"] + [str(f) for f in flacs]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0:
            log.info("✓ ReplayGain успешно рассчитан для альбома %s (%d треков)", root.name, len(flacs))
            return True, f"ReplayGain рассчитан для {len(flacs)} треков"

        batch_err = proc.stderr.strip() or f"Код ошибки: {proc.returncode}"
        last_err = batch_err.splitlines()[-1] if batch_err else "несоответствие форматов"
        log.info(
            "Альбомный расчёт ReplayGain для %s требует адаптивной обработки (%s)",
            root.name, last_err
        )
    except Exception as e:
        log.warning("Сбой пакетного запуска metaflac для %s: %s", root.name, e)

    # 2. Адаптивный фоллбэк: группировка треков по аудиоформату (каналы, частота, разрядность)
    groups: dict[tuple[int, int, int], list[Path]] = {}
    for f in flacs:
        try:
            audio = FLAC(str(f))
            key = (audio.info.channels, audio.info.sample_rate, audio.info.bits_per_sample)
        except Exception:
            key = (2, 44100, 16)
        groups.setdefault(key, []).append(f)

    success_count = 0
    failed_files: list[str] = []

    for fmt_key, group_flacs in groups.items():
        if len(group_flacs) > 1:
            group_cmd = [metaflac_bin, "--add-replay-gain"] + [str(f) for f in group_flacs]
            group_proc = subprocess.run(group_cmd, capture_output=True, text=True, timeout=120)
            if group_proc.returncode == 0:
                success_count += len(group_flacs)
                continue

        # Потрековый расчёт для файлов группы (или если в группе есть повреждённый файл)
        for f in group_flacs:
            single_cmd = [metaflac_bin, "--add-replay-gain", str(f)]
            single_proc = subprocess.run(single_cmd, capture_output=True, text=True, timeout=30)
            if single_proc.returncode == 0:
                success_count += 1
            else:
                err_msg = single_proc.stderr.strip().splitlines()[-1] if single_proc.stderr else "ошибка декодирования"
                log.warning("Пропуск ReplayGain для %s: %s", f.name, err_msg)
                failed_files.append(f.name)

    if success_count > 0:
        if failed_files:
            log.info(
                "✓ ReplayGain рассчитан для %d из %d треков альбома %s (пропущено повреждённых: %d)",
                success_count, len(flacs), root.name, len(failed_files)
            )
            return True, f"ReplayGain рассчитан для {success_count}/{len(flacs)} треков (пропущено: {len(failed_files)})"
        else:
            log.info(
                "✓ ReplayGain успешно рассчитан (адаптивно по форматам) для всех %d треков альбома %s",
                success_count, root.name
            )
            return True, f"ReplayGain рассчитан для {success_count} треков"

    err = f"Не удалось рассчитать ReplayGain для {root.name}"
    log.error(err)
    return False, err


def remove_replaygain(album_dir: str | Path) -> tuple[bool, str]:
    """Удаляет ReplayGain теги из всех FLAC в папке."""
    root = Path(album_dir)
    flacs = sorted(root.rglob("*.flac"))
    if not flacs:
        return False, "В папке нет FLAC-файлов"

    metaflac_bin = find_metaflac()
    if not metaflac_bin:
        return False, "Утилита metaflac не найдена"

    cmd = [metaflac_bin, "--remove-replay-gain"] + [str(f) for f in flacs]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return proc.returncode == 0, "ReplayGain удалён" if proc.returncode == 0 else proc.stderr
    except Exception as e:
        return False, str(e)


def audit_library_replaygain(library_dir: str | Path) -> dict:
    """Аудит наличия ReplayGain по всей библиотеке."""
    root = Path(library_dir)
    if not root.exists():
        return {"total": 0, "with_gain": 0, "missing": 0, "albums_missing": []}

    all_albums = sorted([
        d for d in root.iterdir()
        if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.rglob("*.flac") if f.is_file())
    ])

    total_tracks = 0
    with_gain_tracks = 0
    missing_gain_tracks = 0
    albums_missing = []

    for alb in all_albums:
        flacs = sorted(alb.rglob("*.flac"))
        if not flacs:
            continue
        total_tracks += len(flacs)

        alb_has_missing = False
        for f in flacs:
            if has_replaygain(f):
                with_gain_tracks += 1
            else:
                missing_gain_tracks += 1
                alb_has_missing = True

        if alb_has_missing:
            albums_missing.append(alb)

    return {
        "total": total_tracks,
        "with_gain": with_gain_tracks,
        "missing": missing_gain_tracks,
        "albums_missing": albums_missing,
    }
