"""Мониторинг папки Inbox на появление новых релизов (ADR-0011, ADR-0013).

Polling-based watcher: каждые N секунд сканирует INBOX на предмет новых
подпапок, содержащих хотя бы один .flac файл. Когда такая папка найдена
и не менялась в течение порога стабильности (файлы не дописываются),
она передаётся на обработку.

Watcher запускается как asyncio-задача внутри event loop бота.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Callable, Awaitable
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)


def _get_scan_interval() -> int:
    """Интервал сканирования Inbox (секунды)."""
    return int(os.environ.get("NAVISAN_SCAN_INTERVAL", "5"))


def _get_stability_seconds() -> int:
    """Порог стабильности файлов (секунды после последней записи)."""
    return int(os.environ.get("NAVISAN_STABILITY_SECONDS", "5"))


def _folder_has_flac(folder: Path) -> bool:
    """Есть ли хотя бы один .flac в папке (включая вложенные подпапки)."""
    try:
        return any(f.suffix.lower() == ".flac" for f in folder.rglob("*.flac") if f.is_file())
    except OSError:
        return False


def _folder_mtime(folder: Path) -> float:
    """Самый свежий mtime среди всех файлов в папке релиза."""
    try:
        mtimes = [f.stat().st_mtime for f in folder.rglob("*") if f.is_file()]
        return max(mtimes) if mtimes else 0.0
    except OSError:
        return 0.0


async def watch_inbox(
    inbox: Path,
    on_new_release: Callable[[Path], Awaitable[None]],
    is_busy: Callable[[], bool] | None = None,
) -> None:
    """Бесконечный цикл мониторинга Inbox.

    Args:
        inbox: Путь к папке Inbox (NAVISAN_INBOX).
        on_new_release: Async-callback для запуска обработки папки.
        is_busy: Функция проверки занятости (например, если пользователь
                 прямо сейчас выбирает обложку для предыдущего релиза).
    """
    processing: set[str] = set()

    scan_interval = _get_scan_interval()
    stability_seconds = _get_stability_seconds()
    log.info("Inbox watcher запущен: %s (интервал %ds, порог стабильности %ds)",
             inbox, scan_interval, stability_seconds)

    while True:
        await asyncio.sleep(_get_scan_interval())

        if not inbox.exists():
            continue

        # Если бот занят текущим релизом — ждём его завершения
        if is_busy and is_busy():
            continue

        try:
            entries = sorted([e for e in inbox.iterdir() if e.is_dir()])
        except OSError as exc:
            log.warning("Не удалось прочитать Inbox: %s", exc)
            continue

        # Очищаем из processing папки, которых уже нет в Inbox
        current_names = {e.name for e in entries}
        processing.intersection_update(current_names)

        now = time.time()
        stability = _get_stability_seconds()

        for entry in entries:
            if entry.name in processing:
                continue

            if not _folder_has_flac(entry):
                continue

            # Проверяем стабильность: не дописываются ли ещё файлы
            latest_mtime = _folder_mtime(entry)
            age = now - latest_mtime
            if age < stability:
                log.debug("Inbox: '%s' ещё записывается (%.1fs < %ds)",
                          entry.name, age, stability)
                continue

            # Папка готова к обработке
            processing.add(entry.name)
            log.info("Inbox: обнаружен готовый релиз → %s", entry.name)

            try:
                await on_new_release(entry)
            except Exception:
                log.exception("Ошибка обработки нового релиза %s", entry.name)
            finally:
                # Освобождаем из processing, если папку переместили
                if not entry.exists():
                    processing.discard(entry.name)

            # Обрабатываем релизы по очереди, прерываем цикл текущей итерации
            break

