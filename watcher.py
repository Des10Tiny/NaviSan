"""Мониторинг папки Inbox на появление новых релизов (ADR-0011, ADR-0013).

Polling-based watcher: каждые N секунд сканирует INBOX на предмет новых
подпапок, содержащих хотя бы один .flac файл. Когда такая папка найдена
и не менялась в течение порога стабильности (файлы не дописываются),
она считается готовой к обработке.

Watcher запускается как asyncio-задача внутри event loop бота.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, Awaitable

log = logging.getLogger(__name__)

# Интервал сканирования Inbox (секунды).
SCAN_INTERVAL = int(os.environ.get("NAVISAN_SCAN_INTERVAL", "30"))

# Сколько секунд папка должна не меняться, прежде чем мы её подхватим.
# Защищает от того, что slskd ещё не докачал все треки.
STABILITY_SECONDS = int(os.environ.get("NAVISAN_STABILITY_SECONDS", "60"))


def _folder_has_flac(folder: Path) -> bool:
    """Есть ли хотя бы один .flac в папке (без рекурсии в подпапки)."""
    try:
        return any(f.suffix.lower() == ".flac" for f in folder.iterdir() if f.is_file())
    except OSError:
        return False


def _folder_mtime(folder: Path) -> float:
    """Самый свежий mtime среди файлов в папке (не рекурсивно)."""
    try:
        mtimes = [f.stat().st_mtime for f in folder.iterdir() if f.is_file()]
        return max(mtimes) if mtimes else 0.0
    except OSError:
        return 0.0


async def watch_inbox(
    inbox: Path,
    on_new_release: Callable[[Path], Awaitable[None]],
) -> None:
    """Бесконечный цикл мониторинга Inbox.

    Args:
        inbox: Путь к папке Inbox (NAVISAN_INBOX).
        on_new_release: Async-callback, вызываемый для каждой новой папки
            с FLAC-файлами. Получает Path к этой папке.
    """
    known: set[str] = set()
    # Запоминаем то, что уже было в inbox при старте — не обрабатываем повторно.
    # Если пользователь хочет перезапустить обработку — у него есть /process.
    if inbox.exists():
        for entry in inbox.iterdir():
            if entry.is_dir():
                known.add(entry.name)
        if known:
            log.info("Inbox: %d существующих папок пропущены при старте", len(known))

    log.info("Inbox watcher запущен: %s (интервал %ds, стабильность %ds)",
             inbox, SCAN_INTERVAL, STABILITY_SECONDS)

    while True:
        await asyncio.sleep(SCAN_INTERVAL)

        if not inbox.exists():
            continue

        try:
            entries = [e for e in inbox.iterdir() if e.is_dir()]
        except OSError as exc:
            log.warning("Не удалось прочитать Inbox: %s", exc)
            continue

        import time
        now = time.time()

        for entry in entries:
            if entry.name in known:
                continue

            if not _folder_has_flac(entry):
                continue

            # Проверяем стабильность: если файлы ещё пишутся — пропускаем.
            age = now - _folder_mtime(entry)
            if age < STABILITY_SECONDS:
                log.debug("Inbox: %s ещё не стабильна (%.0fs < %ds)",
                          entry.name, age, STABILITY_SECONDS)
                continue

            # Папка стабильна и содержит FLAC — это новый релиз.
            known.add(entry.name)
            log.info("Inbox: обнаружен новый релиз — %s", entry.name)

            try:
                await on_new_release(entry)
            except Exception:
                log.exception("Ошибка обработки нового релиза %s", entry.name)
