"""Поиск, загрузка и управление текстами песен (.lrc / plain text).

Каскадный водопад поиска (Waterfall):
  1. Синхронизированный текст (Караоке с таймкодами):
     - LRCLIB (synced)
     - Lyricsify (synced)
     - Яндекс Музыка (synced)
  2. Обычный текст (Plain, если караоке не найдено нигде):
     - LRCLIB (plain)
     - Genius (plain)
     - Яндекс Музыка (plain)
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from mutagen.flac import FLAC

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LyricResult:
    """Результат поиска текста песни."""
    text: str                # Содержимое (LRC с таймкодами или обычный текст)
    is_synced: bool          # True, если текст синхронизированный (караоке)
    source: str              # "lrclib" | "lyricsify" | "yandex" | "genius" | "manual"
    track_title: str = ""
    artist: str = ""


def has_timestamps(text: str) -> bool:
    """Проверяет, содержит ли текст временные метки вида [mm:ss.xx]."""
    return bool(re.search(r"\[\d{2}:\d{2}", text))


def preview_lyrics(text: str, max_lines: int = 6) -> str:
    """Возвращает превью первых N строк текста."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    preview = lines[:max_lines]
    if len(lines) > max_lines:
        preview.append("…")
    return "\n".join(preview)


class LyricManager:
    """Управление поиском, конвертацией и сохранением текстов песен."""

    LRCLIB_UA = "NaviSan/1.0 (https://github.com/arsen/navisan)"
    BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    def __init__(self, yandex_token: str | None = None) -> None:
        self.yandex_token = yandex_token or os.environ.get("YANDEX_MUSIC_TOKEN")

    # ── 1. LRCLIB ─────────────────────────────────────────────────────────────

    def _fetch_lrclib(
        self,
        artist: str,
        title: str,
        album: str = "",
        duration: int = 0,
    ) -> tuple[LyricResult | None, LyricResult | None]:
        """Ищет текст в LRCLIB. Возвращает кортеж (synced_result, plain_result)."""
        headers = {"User-Agent": self.LRCLIB_UA}
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
            log.debug("LRCLIB exact error: %s", e)

        # Мягкий поиск
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

    # ── 2. Lyricsify ──────────────────────────────────────────────────────────

    def _fetch_lyricsify(self, artist: str, title: str) -> LyricResult | None:
        """Ищет текст на Lyricsify.com."""
        headers = {
            "User-Agent": self.BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.lyricsify.com/",
        }
        try:
            r = requests.get(
                "https://www.lyricsify.com/search",
                params={"q": f"{artist} {title}"},
                headers=headers,
                timeout=8,
            )
            if r.status_code != 200:
                return None

            soup = BeautifulSoup(r.text, "html.parser")
            # Находим ссылки на треки в результатах поиска
            link = soup.find("a", href=lambda h: h and h.startswith("/lyric/"))
            if not link:
                return None

            track_url = f"https://www.lyricsify.com{link['href']}"
            track_resp = requests.get(track_url, headers=headers, timeout=8)
            if track_resp.status_code != 200:
                return None

            t_soup = BeautifulSoup(track_resp.text, "html.parser")
            lyrics_box = t_soup.find("div", id="lyrics_box") or t_soup.find("pre")
            if lyrics_box:
                text = lyrics_box.get_text().strip()
                if text:
                    is_sync = has_timestamps(text)
                    return LyricResult(text=text, is_synced=is_sync, source="lyricsify", track_title=title, artist=artist)
        except Exception as e:
            log.debug("Lyricsify error: %s", e)

        return None

    # ── 3. Яндекс Музыка ──────────────────────────────────────────────────────

    def _fetch_yandex(self, artist: str, title: str) -> tuple[LyricResult | None, LyricResult | None]:
        """Ищет текст на Яндекс Музыке. Возвращает (synced_result, plain_result)."""
        headers = {
            "User-Agent": "YandexMusic/2024.08.1 (Android; 14; sdk 34)",
            "Accept": "application/json",
        }
        if self.yandex_token:
            headers["Authorization"] = f"OAuth {self.yandex_token}"

        try:
            r = requests.get(
                "https://api.music.yandex.net/search",
                params={"text": f"{artist} {title}", "type": "track", "page": 0},
                headers=headers,
                timeout=8,
            )
            if r.status_code != 200:
                return None, None

            tracks = r.json().get("result", {}).get("tracks", {}).get("results", [])
            if not tracks:
                return None, None

            track_id = tracks[0].get("id")
            if not track_id:
                return None, None

            supp_r = requests.get(
                f"https://api.music.yandex.net/tracks/{track_id}/supplement",
                headers=headers,
                timeout=8,
            )
            if supp_r.status_code != 200:
                return None, None

            lyrics_info = supp_r.json().get("result", {}).get("lyrics", {})
            if not lyrics_info:
                return None, None

            sync_type = lyrics_info.get("syncType", "").upper()
            down_url = lyrics_info.get("downloadUrl")
            full_lyrics = lyrics_info.get("fullLyrics", "")

            # Синхронизированный текст
            if sync_type in ("LINE", "WORD") and down_url:
                down_r = requests.get(down_url, headers=headers, timeout=8)
                if down_r.status_code == 200:
                    lrc_text = self._convert_yandex_to_lrc(down_r.text)
                    if lrc_text:
                        synced_res = LyricResult(
                            text=lrc_text, is_synced=True, source="yandex", track_title=title, artist=artist
                        )
                        plain_res = (
                            LyricResult(text=full_lyrics, is_synced=False, source="yandex", track_title=title, artist=artist)
                            if full_lyrics
                            else None
                        )
                        return synced_res, plain_res

            if full_lyrics:
                return None, LyricResult(
                    text=full_lyrics, is_synced=False, source="yandex", track_title=title, artist=artist
                )
        except Exception as e:
            log.debug("Yandex music lyrics error: %s", e)

        return None, None

    def _convert_yandex_to_lrc(self, content: str) -> str:
        """Преобразует XML от Яндекс Музыки в стандартный LRC."""
        if content.strip().startswith("<"):
            try:
                root = ET.fromstring(content)
                lines = []
                for line in root.findall(".//line"):
                    start_ms = int(line.get("start", 0))
                    text = (line.text or "").strip()
                    m = start_ms // 60000
                    s = (start_ms % 60000) // 1000
                    cs = (start_ms % 1000) // 10
                    lines.append(f"[{m:02d}:{s:02d}.{cs:02d}] {text}")
                return "\n".join(lines)
            except Exception:
                pass

        if has_timestamps(content):
            return content.strip()
        return ""

    # ── 4. Genius ─────────────────────────────────────────────────────────────

    def _fetch_genius(self, artist: str, title: str) -> LyricResult | None:
        """Ищет эталонный текст песни на Genius.com."""
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

            # Ищем наиболее точный хит, отсеивая переводы (Translations)
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

            # Удаляем мусорные блоки шапки Genius и сайдбаров
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

    # ── Каскадный поиск (Waterfall) ───────────────────────────────────────────

    def find_lyrics(
        self,
        artist: str,
        title: str,
        album: str = "",
        duration: int = 0,
        preferred_source: str | None = None,
    ) -> LyricResult | None:
        """Ищет текст песни по водопаду или по конкретному источнику."""
        # 1. Прямой поиск по указанному источнику
        if preferred_source == "lrclib":
            synced, plain = self._fetch_lrclib(artist, title, album, duration)
            return synced or plain
        if preferred_source == "genius":
            return self._fetch_genius(artist, title)
        if preferred_source == "yandex":
            synced, plain = self._fetch_yandex(artist, title)
            return synced or plain
        if preferred_source == "lyricsify":
            return self._fetch_lyricsify(artist, title)

        # 2. Автоматический каскад:
        # ── ФАЗА 1: Синхронизированный текст (Караоке с таймкодами) ──
        log.info("Поиск караоке для: %s — %s", artist, title)

        # Шаг 1. LRCLIB (synced)
        lrclib_synced, lrclib_plain = self._fetch_lrclib(artist, title, album, duration)
        if lrclib_synced:
            log.info("  ✓ Найдено караоке [LRCLIB]: %s — %s", artist, title)
            return lrclib_synced

        # Шаг 2. Lyricsify (synced)
        lyricsify_res = self._fetch_lyricsify(artist, title)
        if lyricsify_res and lyricsify_res.is_synced:
            log.info("  ✓ Найдено караоке [Lyricsify]: %s — %s", artist, title)
            return lyricsify_res

        # Шаг 3. Яндекс Музыка (synced)
        yandex_synced, yandex_plain = self._fetch_yandex(artist, title)
        if yandex_synced:
            log.info("  ✓ Найдено караоке [Яндекс]: %s — %s", artist, title)
            return yandex_synced

        # ── ФАЗА 2: Обычный текст (Plain), если караоке не найдено нигде ──
        log.info("Караоке не найдено. Ищу обычный текст: %s — %s", artist, title)

        # Шаг 4. LRCLIB (plain)
        if lrclib_plain:
            log.info("  ✓ Найден текст [LRCLIB]: %s — %s", artist, title)
            return lrclib_plain

        # Шаг 5. Genius (plain)
        genius_plain = self._fetch_genius(artist, title)
        if genius_plain:
            log.info("  ✓ Найден текст [Genius]: %s — %s", artist, title)
            return genius_plain

        # Шаг 6. Яндекс Музыка (plain)
        if yandex_plain:
            log.info("  ✓ Найден текст [Яндекс]: %s — %s", artist, title)
            return yandex_plain

        # Шаг 7. Несинхронизированный Lyricsify (если был)
        if lyricsify_res:
            return lyricsify_res

        log.info("  ❌ Текст не найден ни на одном сервисе: %s — %s", artist, title)
        return None

    # ── Обработка файла ───────────────────────────────────────────────────────

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
            # Если уже есть файл, определяем его тип для красивого отчёта
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
                source_label = {
                    "lrclib": "LRCLIB",
                    "yandex": "Яндекс",
                    "genius": "Genius",
                    "lyricsify": "Lyricsify",
                    "manual": "Вручную",
                }.get(res.source, res.source.upper())

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
