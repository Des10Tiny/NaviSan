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

    # ── 3. LRCMUX (KuGou, Musixmatch, NetEase, YouTube Music) ─────────────────

    def _fetch_lrcmux(
        self,
        artist: str,
        title: str,
        album: str = "",
        duration: int = 0,
    ) -> LyricResult | None:
        """Ищет синхронизированный текст через мультиплексор lrcmux (KuGou, Musixmatch, NetEase, YTMusic)."""
        artist_clean = artist.strip()
        title_clean = title.strip()
        if not artist_clean or not title_clean:
            return None

        params = {"artist": artist_clean, "title": title_clean}
        if album.strip():
            params["album"] = album.strip()
        if duration > 0:
            params["duration"] = str(duration)

        try:
            r = requests.get(
                "https://api.lrcmux.dev/get",
                params=params,
                headers={"User-Agent": self.BROWSER_UA},
                timeout=6,
            )
            if r.status_code == 200:
                data = r.json()
                lines = data.get("lines", [])
                if lines:
                    out = []
                    has_sync = False
                    for line in lines:
                        txt = line.get("text", "").strip()
                        start_ms = line.get("start")
                        if start_ms is not None:
                            has_sync = True
                            total_sec = max(0.0, start_ms / 1000.0)
                            m = int(total_sec // 60)
                            s = total_sec % 60
                            out.append(f"[{m:02d}:{s:05.2f}] {txt}")
                        elif txt:
                            out.append(txt)

                    full_text = "\n".join(out).strip()
                    if full_text:
                        return LyricResult(
                            text=full_text,
                            is_synced=has_sync,
                            source="lrcmux",
                            track_title=title,
                            artist=artist,
                        )
        except Exception as e:
            log.debug("lrcmux fetch error for '%s — %s': %s", artist, title, e)

        return None

    # ── 4. SyncedLyrics (NetEase, Megalobiz, Musixmatch) ─────────────────────

    def _fetch_syncedlyrics(self, artist: str, title: str) -> LyricResult | None:
        """Ищет синхронизированный караоке-текст через провайдеры syncedlyrics."""
        search_query = f"{artist} {title}".strip()
        if not search_query:
            return None

        try:
            import syncedlyrics
            from syncedlyrics.providers.base import TimeoutSession

            # Увеличиваем таймаут подключения до 5 секунд для надёжной связи с зарубежными базами
            def _custom_timeout_request(session_self, method, url, **kwargs):
                kwargs["timeout"] = (5, 12)
                return super(TimeoutSession, session_self).request(method, url, **kwargs)

            TimeoutSession.request = _custom_timeout_request

            # Ищем исключительно синхронизированный текст (с таймкодами)
            lrc_text = syncedlyrics.search(search_query, synced_only=True)
            if lrc_text and has_timestamps(lrc_text):
                return LyricResult(
                    text=lrc_text.strip(),
                    is_synced=True,
                    source="syncedlyrics",
                    track_title=title,
                    artist=artist,
                )
        except Exception as e:
            log.debug("syncedlyrics error for '%s — %s': %s", artist, title, e)

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
        if preferred_source == "lrcmux":
            return self._fetch_lrcmux(artist, title, album, duration)
        if preferred_source == "syncedlyrics":
            return self._fetch_syncedlyrics(artist, title)
        if preferred_source == "genius":
            return self._fetch_genius(artist, title)

        # 2. Автоматический каскад:
        # Фаза 1: Синхронизированный текст (LRCLIB)
        lrclib_synced, lrclib_plain = self._fetch_lrclib(artist, title, album, duration)
        if lrclib_synced:
            log.info("  ✓ Найдено караоке [LRCLIB]: %s — %s", artist, title)
            return lrclib_synced

        # Фаза 2: Синхронизированный текст через мультиплексор lrcmux (KuGou, Musixmatch, NetEase, YouTube Music)
        lrcmux_res = self._fetch_lrcmux(artist, title, album, duration)
        if lrcmux_res and lrcmux_res.is_synced:
            log.info("  ✓ Найдено караоке [lrcmux]: %s — %s", artist, title)
            return lrcmux_res

        # Фаза 3: Синхронизированный текст через мульти-провайдер syncedlyrics
        synced_res = self._fetch_syncedlyrics(artist, title)
        if synced_res and synced_res.is_synced:
            log.info("  ✓ Найдено караоке [SyncedLyrics]: %s — %s", artist, title)
            return synced_res

        # Фаза 4: Обычный текст (Plain) — из первого запроса LRCLIB
        if lrclib_plain:
            log.info("  ✓ Найден текст [LRCLIB]: %s — %s", artist, title)
            return lrclib_plain

        # Фаза 5: Обычный текст из lrcmux (если не было синхронизации)
        if lrcmux_res and lrcmux_res.text:
            log.info("  ✓ Найден текст [lrcmux]: %s — %s", artist, title)
            return lrcmux_res

        # Фаза 6: Эталонный plain-текст с Genius
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
            "Синхронизированный [LRCLIB] 🎤" | "Обычный текст [Genius] 📄" | "Уже есть 📁" | "Не найдено ❌"
        """
        path = Path(filepath)
        if path.suffix.lower() != ".flac":
            return "Не аудиофайл"

        lrc_path = path.with_suffix(".lrc")
        if not overwrite and lrc_path.exists():
            try:
                content = lrc_path.read_text(encoding="utf-8")
                return "Синхронизированный 🎤" if has_timestamps(content) else "Обычный текст 📄"
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
                source_labels = {
                    "lrclib": "LRCLIB",
                    "lrcmux": "LRCMUX",
                    "syncedlyrics": "SyncedLyrics",
                    "genius": "Genius",
                    "manual": "Вручную",
                }
                source_label = source_labels.get(res.source, res.source.upper())

                if res.is_synced:
                    return f"Синхронизированный [{source_label}] 🎤"
                return f"Обычный текст [{source_label}] 📄"

            return "Не найдено ❌"
        except Exception as e:
            log.error("Ошибка при обработке текста для %s: %s", path.name, e)
            return f"Ошибка: {e}"

    def ai_align_track(self, flac_path: str | Path, model: any = None) -> tuple[LyricResult | None, str]:
        """Выполняет нейро-выравнивание текста по аудиофайлу трека (Forced Alignment).

        1. Берёт существующий plain-текст из .lrc (если он уже скачан с Genius).
        2. Если текста нет, скачивает plain-текст через водопад (Genius / lrcmux).
        3. Запускает align_lyrics() для расстановки таймкодов.
        4. Сохраняет обновлённый синхронизированный .lrc файл.

        Returns:
            (LyricResult, "Сообщение о статусе")
        """
        path = Path(flac_path)
        if not path.exists() or path.suffix.lower() != ".flac":
            return None, "Файл не найден или не является FLAC"

        lrc_path = path.with_suffix(".lrc")
        plain_text = ""

        # 1. Проверяем существующий файл .lrc
        if lrc_path.exists():
            try:
                content = lrc_path.read_text(encoding="utf-8").strip()
                # Если уже есть таймкоды — очищаем их, чтобы получить чистый текст для выравнивания
                lines = []
                for l in content.split("\n"):
                    cleaned = re.sub(r"\[\d{2}:\d{2}\.\d+\]\s*", "", l).strip()
                    if cleaned:
                        lines.append(cleaned)
                plain_text = "\n".join(lines)
            except Exception as e:
                log.debug("Ошибка чтения lrc %s: %s", lrc_path, e)

        # 2. Если текста не было, скачиваем с Genius/водопада
        if not plain_text:
            try:
                audio = FLAC(str(path))
                artist = audio.get("artist", audio.get("albumartist", [""]))[0]
                title = audio.get("title", [""])[0]
                if artist and title:
                    res = self._fetch_genius(artist, title) or self._fetch_lrcmux(artist, title)
                    if res and res.text:
                        plain_text = res.text
            except Exception as e:
                log.debug("Ошибка получения тегов трека %s: %s", path, e)

        if not plain_text.strip():
            return None, "Текст песни не найден ни в файле, ни на Genius"

        # 3. Выравнивание через faster-whisper
        try:
            from aligner import align_lyrics
            synced_text = align_lyrics(path, plain_text, model=model)
            if synced_text and has_timestamps(synced_text):
                lrc_path.write_text(synced_text, encoding="utf-8")
                res = LyricResult(
                    text=synced_text,
                    is_synced=True,
                    source="ai",
                    track_title=path.stem,
                )
                return res, "Таймкоды успешно расставлены нейросетью ✨"
            return None, "Нейросети не удалось распознать слова в аудио"
        except Exception as e:
            log.error("Ошибка при AI-выравнивании %s: %s", path.name, e)
            return None, f"Ошибка нейросети: {e}"

    def save_manual_lyrics(self, lrc_path: Path, text: str) -> LyricResult:
        """Сохраняет вручную введённый текст в файл .lrc."""
        text = text.strip()
        is_sync = has_timestamps(text)
        lrc_path.write_text(text, encoding="utf-8")
        return LyricResult(text=text, is_synced=is_sync, source="manual")


def audit_library_lyrics(library_dir: str | Path) -> dict:
    """Аудит наличия и синхронизации текстов песен по всей музыкальной библиотеке.

    Returns:
        Словарь с ключами:
        - total: общее число FLAC треков
        - synced: список Path треков с синхронизированным .lrc
        - plain: список Path треков с обычным текстом .lrc (готовы к AI-выравниванию)
        - missing: список Path треков без .lrc
        - albums_plain: словарь {album_dir_name: [Path, ...]} для удобного пакетного выравнивания
    """
    root = Path(library_dir)
    if not root.exists():
        return {"total": 0, "synced": [], "plain": [], "missing": [], "albums_plain": {}}

    flac_files = sorted(root.rglob("*.flac"))
    synced: list[Path] = []
    plain: list[Path] = []
    missing: list[Path] = []
    albums_plain: dict[str, list[Path]] = {}

    for flac in flac_files:
        lrc = flac.with_suffix(".lrc")
        if not lrc.exists():
            missing.append(flac)
            continue

        try:
            content = lrc.read_text(encoding="utf-8", errors="ignore")
            if has_timestamps(content):
                synced.append(flac)
            elif content.strip():
                plain.append(flac)
                alb_name = flac.parent.name
                albums_plain.setdefault(alb_name, []).append(flac)
            else:
                missing.append(flac)
        except Exception:
            missing.append(flac)

    return {
        "total": len(flac_files),
        "synced": synced,
        "plain": plain,
        "missing": missing,
        "albums_plain": albums_plain,
    }
