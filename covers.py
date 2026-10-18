"""Поиск и скачивание обложек через MusicHoarders API (ADR-0012).

Модуль отправляет HTTP POST на covers.musichoarders.xyz, читает NDJSON-поток
с результатами из нескольких стриминговых сервисов, дедуплицирует кандидатов
по URL полноразмерного изображения и возвращает готовый список.

Скачивание выбранной обложки — отдельная функция: бот вызывает её после того,
как пользователь нажал кнопку в Telegram.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# ── Константы ────────────────────────────────────────────────────────────────

API_URL = "https://covers.musichoarders.xyz/api/search"

# Apple Music исключён: CDN отдаёт WebP под расширением .jpg (ADR-0012).
DEFAULT_SOURCES: tuple[str, ...] = ("amazonmusic", "deezer", "spotify", "tidal")
DEFAULT_COUNTRY = "us"

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:157.0) "
    "Gecko/20100101 Firefox/157.0"
)

# Таймауты (секунды).
# connect — время на установку TCP-соединения.
# stream  — максимальная пауза между NDJSON-строками; если источник завис,
#           через это время iter_lines бросит Timeout, и мы вернём то, что есть.
# download — полное время на скачивание одного изображения.
_CONNECT_TIMEOUT = 10
_STREAM_TIMEOUT = 30
_DOWNLOAD_TIMEOUT = 30


# ── Модель ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CoverCandidate:
    """Один кандидат обложки, полученный от API.

    Поля ``date`` и ``tracks`` могут отсутствовать — не все источники
    их возвращают.
    """

    small_url: str          # превью ≤480 px — для Telegram MediaGroup
    big_url: str            # полноразмерный оригинал — для вшивания в FLAC
    source: str             # amazonmusic | deezer | spotify | tidal
    title: str              # название релиза из источника
    artist: str             # имя артиста из источника
    date: str | None = None         # "YYYY" или "YYYY-MM-DD"
    tracks: int | None = None       # количество треков (если есть)
    release_url: str | None = None  # ссылка на страницу релиза


# ── Поиск ────────────────────────────────────────────────────────────────────

def search_covers(
    artist: str,
    album: str,
    *,
    country: str = DEFAULT_COUNTRY,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
) -> list[CoverCandidate]:
    """Ищет обложки альбома по всем указанным источникам.

    Отправляет POST-запрос, читает NDJSON-поток до получения ``done`` от
    каждого источника, дедуплицирует результаты по ``big_url``.

    Возвращает пустой список при сетевых ошибках или если ничего не найдено.
    Если один из источников завис и вызвал таймаут между строками — возвращает
    то, что успело прийти от остальных.
    """
    session_id = uuid.uuid4().hex

    headers = {
        "User-Agent": _BROWSER_UA,
        "Content-Type": "application/json",
        "Referer": "https://covers.musichoarders.xyz/",
        "Origin": "https://covers.musichoarders.xyz",
        "x-session": session_id,
    }

    payload = {
        "artist": artist,
        "album": album,
        "country": country,
        "sources": list(sources),
    }

    log.info("Поиск обложек: %s — %s (источники: %s)", artist, album, ", ".join(sources))

    try:
        resp = requests.post(
            API_URL,
            headers=headers,
            json=payload,
            stream=True,
            timeout=(_CONNECT_TIMEOUT, _STREAM_TIMEOUT),
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Ошибка соединения с covers API: %s", exc)
        return []

    candidates: list[CoverCandidate] = []
    seen_urls: set[str] = set()
    sources_pending = set(sources)

    try:
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue

            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                log.debug("Пропущена невалидная JSON-строка: %s", raw_line[:120])
                continue

            msg_type = obj.get("type")

            if msg_type == "cover":
                big_url = obj.get("bigCoverUrl", "")
                if not big_url or big_url in seen_urls:
                    continue
                seen_urls.add(big_url)

                info = obj.get("releaseInfo", {})
                candidate = CoverCandidate(
                    small_url=obj.get("smallCoverUrl", ""),
                    big_url=big_url,
                    source=obj.get("source", "unknown"),
                    title=info.get("title", ""),
                    artist=info.get("artist", "").strip(),
                    date=info.get("date"),
                    tracks=info.get("tracks"),
                    release_url=info.get("url"),
                )
                candidates.append(candidate)
                log.debug("  ← %s: %s (%s)", candidate.source, candidate.title, candidate.date or "—")

            elif msg_type == "done":
                done_source = obj.get("source", "")
                success = obj.get("success", False)
                sources_pending.discard(done_source)
                log.debug("  ✓ %s done (success=%s), осталось: %d", done_source, success, len(sources_pending))
                if not sources_pending:
                    break

    except requests.exceptions.Timeout:
        # Один из источников завис — возвращаем, что успело прийти.
        remaining = ", ".join(sources_pending) or "—"
        log.warning("Таймаут потока, не дождались: %s. Возвращаем %d кандидатов.", remaining, len(candidates))

    except requests.RequestException as exc:
        log.error("Ошибка чтения потока: %s", exc)

    finally:
        resp.close()

    log.info("Найдено %d кандидатов (из %d уникальных URL)", len(candidates), len(seen_urls))
    return candidates


# ── Скачивание ───────────────────────────────────────────────────────────────

def download_cover(url: str, dest: Path) -> Path:
    """Скачивает полноразмерную обложку и сохраняет на диск.

    Args:
        url: Адрес ``bigCoverUrl`` выбранного кандидата.
        dest: Путь для сохранения (обычно ``album_folder / "cover.jpg"``).

    Returns:
        Путь к сохранённому файлу (== *dest*).

    Raises:
        requests.RequestException: Сетевая ошибка при скачивании.
        ValueError: Сервер вернул пустое тело ответа.
    """
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "image/*,*/*;q=0.8",
    }

    log.info("Скачивание обложки: %s", url)

    resp = requests.get(url, headers=headers, timeout=_DOWNLOAD_TIMEOUT)
    resp.raise_for_status()

    if not resp.content:
        raise ValueError(f"Пустой ответ при скачивании обложки: {url}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)

    size_kb = len(resp.content) // 1024
    log.info("Обложка сохранена: %s (%d KB)", dest.name, size_kb)

    return dest
