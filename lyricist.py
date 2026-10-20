"""Поиск, загрузка и управление текстами песен (.lrc / plain text).

Каскадный водопад поиска:
  1. LRCLIB (synced) — синхронизированный караоке-текст с таймкодами.
  2. LRCLIB (plain)  — обычный текст без таймкодов, если караоке не найдено.
  3. Genius (plain)  — эталонный текст с Genius.com как надёжный универсальный фоллбэк.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from mutagen.flac import FLAC

log = logging.getLogger(__name__)


# ── Модель ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class LyricResult:
    """Результат поиска текста песни."""

    text: str                # Содержимое (LRC с таймкодами или обычный текст)
    is_synced: bool          # True, если текст синхронизированный (караоке)
    source: str              # "lrclib" | "genius" | "manual"
    track_title: str = ""
    artist: str = ""


# ── Вспомогательные функции ──────────────────────────────────────────────────

def has_timestamps(text: str) -> bool:
    """Проверяет, содержит ли текст временные метки караоке вида [mm:ss.xx]."""
    return bool(re.search(r"\[\d{2}:\d{2}", text))


def preview_lyrics(text: str, max_lines: int = 6) -> str:
    """Возвращает компактное превью первых N строк текста."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    preview = lines[:max_lines]
    if len(lines) > max_lines:
        preview.append("…")
    return "\n".join(preview)


# ── Менеджер текстов ──────────────────────────────────────────────────────────

class LyricManager:
    """Поиск, загрузка и форматирование текстов песен."""

    LRCLIB_UA = "NaviSan/1.0 (https://github.com/arsen/navisan)"
    BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    # ── 1. LRCLIB ─────────────────────────────────────────────────────────────

    def _fetch_lrclib(
        self,
        artist: str,
        title: str,
        album: str = "",
        duration: int = 0,
    ) -> tuple[LyricResult | None, LyricResult | None]:
        """Ищет текст в LRCLIB через точный и мягкий поиск.

        Returns:
            Кортеж (synced_result, plain_result).
        """
        headers = {"User-Agent": self.LRCLIB_UA}

        # Шаг 1: Точный поиск по метаданным
        exact_params = {
            "artist_name": artist,
            "track_name": title,
            "album_name": album,
            "duration": duration,
        }
        try:
            r = requests.get(
                "https://lrclib.net/api/get",
                params=exact_params,
                headers=headers,
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json()
                synced = data.get("syncedLyrics")
                plain = data.get("plainLyrics")
                res_synced = (
                    LyricResult(text=synced, is_synced=True, source="lrclib", track_title=title, artist=artist)
                    if synced
                    else None
                )
                res_plain = (
                    LyricResult(text=plain, is_synced=False, source="lrclib", track_title=title, artist=artist)
                    if plain
                    else None
                )
                if res_synced or res_plain:
                    return res_synced, res_plain
        except Exception as e:
            log.debug("LRCLIB exact match error: %s", e)

        # Шаг 2: Мягкий поиск по ключевым словам
        try:
            r = requests.get(
                "https://lrclib.net/api/search",
                params={"q": f"{artist} {title}"},
                headers=headers,
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    best_plain = None
                    for item in data:
                        api_artist = item.get("artistName", "").lower()
                        api_title = item.get("trackName", "").lower()
                        if artist.lower() not in api_artist and title.lower() not in api_title:
                            continue

                        synced = item.get("syncedLyrics")
                        plain = item.get("plainLyrics")
                        if synced:
                            return (
                                LyricResult(text=synced, is_synced=True, source="lrclib", track_title=title, artist=artist),
                                LyricResult(text=plain, is_synced=False, source="lrclib", track_title=title, artist=artist) if plain else None,
                            )
                        if plain and not best_plain:
                            best_plain = LyricResult(text=plain, is_synced=False, source="lrclib", track_title=title, artist=artist)
                    if best_plain:
                        return None, best_plain
        except Exception as e:
            log.debug("LRCLIB search error: %s", e)

        return None, None

    # ── 2. Genius ─────────────────────────────────────────────────────────────

    def _fetch_genius(self, artist: str, title: str) -> LyricResult | None:
        """Ищет эталонный текст песни на Genius.com без авторизации."""
        headers = {"User-Agent": self.BROWSER_UA}
        try:
            r = requests.get(
                "https://genius.com/api/search/multi",
                params={"q": f"{artist} {title}"},
                headers=headers,
                timeout=10,
            )
            if r.status_code != 200:
                return None

            sections = r.json().get("response", {}).get("sections", [])
            hits = []
            for s in sections:
                if s.get("type") == "song":
                    hits = s.get("hits", [])
                    break
            if not hits:
                return None

            # Отсеиваем переводы (Translations) и выбираем лучшее совпадение по артисту
            best_hit = None
            artist_parts = [p.lower() for p in artist.split() if len(p) > 2]
            for h in hits:
                song = h.get("result", {})
                art_names = (song.get("artist_names", "") + " " + song.get("primary_artist", {}).get("name", "")).lower()
                title_name = song.get("title", "").lower()
                if "translation" in title_name or "translation" in art_names:
                    continue
                if any(part in art_names for part in artist_parts):
                    best_hit = song
                    break

            if not best_hit:
                best_hit = hits[0].get("result", {})

            song_url = best_hit.get("url")
            if not song_url:
                return None

            page = requests.get(song_url, headers=headers, timeout=10)
            if page.status_code != 200:
                return None

            soup = BeautifulSoup(page.text, "html.parser")

            # Удаляем служебные шапки контрибьюторов и сайдбары Genius
            for bad in soup.find_all(class_=lambda cl: cl and any(x in cl for x in ["LyricsHeader", "LyricsEditDesktop", "Sidebar"])):
                bad.decompose()

            for br in soup.find_all("br"):
                br.replace_with("\n")

            containers = soup.find_all("div", attrs={"data-lyrics-container": "true"})
            if not containers:
                return None

            full_text = "\n".join(c.get_text() for c in containers)
            full_text = re.sub(r"\n{3,}", "\n\n", full_text)
            full_text = re.sub(r"\d*Embed$", "", full_text.strip())

            if full_text.strip():
                return LyricResult(
                    text=full_text.strip(),
                    is_synced=False,
                    source="genius",
                    track_title=title,
                    artist=artist,
                )
        except Exception as e:
            log.debug("Genius fetch error: %s", e)

        return None

    # ── Каскадный поиск ───────────────────────────────────────────────────────

    def find_lyrics(
        self,
        artist: str,
        title: str,
        album: str = "",
        duration: int = 0,
        preferred_source: str | None = None,
    ) -> LyricResult | None:
        """Выполняет поиск текста по каскаду или по указанному источнику."""
        # 1. Точечный поиск по выбранному сервису
        if preferred_source == "lrclib":
            synced, plain = self._fetch_lrclib(artist, title, album, duration)
            return synced or plain
        if preferred_source == "genius":
            return self._fetch_genius(artist, title)

        # 2. Автоматический каскад:
        # Фаза 1: Синхронизированный текст (Караоке с таймкодами)
        lrclib_synced, lrclib_plain = self._fetch_lrclib(artist, title, album, duration)
        if lrclib_synced:
            log.info("  ✓ Найдено караоке [LRCLIB]: %s — %s", artist, title)
            return lrclib_synced

        # Фаза 2: Обычный текст (Plain)
        if lrclib_plain:
            log.info("  ✓ Найден текст [LRCLIB]: %s — %s", artist, title)
            return lrclib_plain

        genius_plain = self._fetch_genius(artist, title)
        if genius_plain:
            log.info("  ✓ Найден текст [Genius]: %s — %s", artist, title)
            return genius_plain

        log.info("  ❌ Текст не найден: %s — %s", artist, title)
        return None

    # ── Обработка файлов ──────────────────────────────────────────────────────

    def process_file(self, filepath: str | Path, overwrite: bool = False) -> str:
        """Скачивает текст по водопаду и сохраняет .lrc файл рядом с треком.

        Returns:
            Строку-статус для отчёта:
            "Караоке [LRCLIB] ✨" | "Текст [Genius] 📄" | "Уже есть 📁" | "Не найдено ❌"
        """
        path = Path(filepath)
        if path.suffix.lower() != ".flac":
            return "Не аудиофайл"

        lrc_path = path.with_suffix(".lrc")
        if not overwrite and lrc_path.exists():
            try:
                content = lrc_path.read_text(encoding="utf-8")
                return "Караоке ✨" if has_timestamps(content) else "Текст 📄"
            except Exception:
                return "Уже есть 📁"

        try:
            audio = FLAC(str(path))
            artist = audio.get("artist", audio.get("albumartist", [""]))[0]
            title = audio.get("title", [""])[0]
            album = audio.get("album", [""])[0]
            duration = int(audio.info.length) if hasattr(audio.info, "length") else 0

            if not artist or not title:
                return "Нет тегов ❌"

            res = self.find_lyrics(artist, title, album, duration)
            if res and res.text:
                lrc_path.write_text(res.text, encoding="utf-8")
                source_label = "LRCLIB" if res.source == "lrclib" else "Genius"

                if res.is_synced:
                    return f"Караоке [{source_label}] ✨"
                return f"Текст [{source_label}] 📄"

            return "Не найдено ❌"
        except Exception as e:
            log.error("Ошибка при обработке текста для %s: %s", path.name, e)
            return f"Ошибка: {e}"

    def save_manual_lyrics(self, lrc_path: Path, text: str) -> LyricResult:
        """Сохраняет вручную введённый текст в файл .lrc."""
        text = text.strip()
        is_sync = has_timestamps(text)
        lrc_path.write_text(text, encoding="utf-8")
        return LyricResult(text=text, is_synced=is_sync, source="manual")
