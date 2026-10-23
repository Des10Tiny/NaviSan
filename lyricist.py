"""Поиск, загрузка и управление текстами песен (.lrc / plain text).

Каскадный водопад поиска:
  1. LRCLIB (synced) — синхронизированный караоке-текст с таймкодами.
  2. LRCLIB (plain)  — обычный текст без таймкодов, если караоке не найдено.
  3. Genius (plain)  — эталонный текст с Genius.com как надёжный универсальный фоллбэк.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
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


# Паттерн для китайских метаданных из KuGou/NetEase (作词, 作曲, 编曲 и т.д.)
CJK_META_RE = re.compile(
    r"^(?:\[\d{2}:\d{2}(?:\.\d+)?\]\s*)?(?:(?:作\s*词|作\s*曲|编\s*曲|制\s*作\s*人|录\s*音|混\s*音|母\s*带|监\s*制|和\s*音|企\s*划|出\s*品|发\s*行|词|曲|原\s*唱|翻\s*唱|演\s*唱)\s*[:：\s/\-]|(?:未经.*?许可|不得.*?使用|网易云音乐|QQ音乐|酷狗音乐))",
    re.I,
)


def clean_track_title(t: str) -> str:
    """Удаляет номера треков, префиксы, feat/ft/with/remastered/explicit/etc."""
    t = re.sub(r"^\d+[\s\.\-_]+", "", t or "")
    t = re.sub(
        r"(?i)\s*[\(\[](?:feat\.?|ft\.?|with|prod\.?|official\s+video|explicit|remastered|bonus\s+track).*?[\)\]]",
        "",
        t,
    )
    if " - " in t:
        t = t.split(" - ")[-1]
    return t.strip()


def is_translation(title: str, artist: str) -> bool:
    """Проверяет, является ли кандидат переводом или кавером на другой язык."""
    bad = (
        "translation", "traducción", "traduccion", "tradução", "traducao",
        "çeviri", "ceviri", "romanized", "übersetzung", "traduzione",
        "paroles", "deutsche"
    )
    combined = f"{title} {artist}".lower()
    return any(b in combined for b in bad)


def clean_lrc_metadata(text: str) -> str:
    """Удаляет мусорные служебные строки (китайские кредиты 作词/作曲 из KuGou/NetEase)."""
    if not text:
        return ""
    lines = []
    for l in text.splitlines():
        trimmed = l.strip()
        if not trimmed or CJK_META_RE.match(trimmed):
            continue
        lines.append(trimmed)
    return "\n".join(lines).strip()


# ── Менеджер текстов ──────────────────────────────────────────────────────────

class LyricManager:
    """Поиск, загрузка и форматирование текстов песен."""

    BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    LRCLIB_UA = BROWSER_UA

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
        headers = {"User-Agent": self.BROWSER_UA}

        clean_t = clean_track_title(title)
        artist_tokens = [a.strip() for a in re.split(r"[,;&]|\s+(?:feat\.?|ft\.?|x|and)\s+", artist, flags=re.I) if a.strip()]
        first_art = artist_tokens[0] if artist_tokens else artist

        # Шаг 1: Точный поиск по метаданным (пробуем полный артист и первого артиста)
        exact_trials = [
            {"artist_name": artist, "track_name": title, "album_name": album, "duration": duration},
            {"artist_name": first_art, "track_name": clean_t, "album_name": album},
        ]
        if clean_t != title:
            exact_trials.insert(1, {"artist_name": artist, "track_name": clean_t, "album_name": album})

        for exact_params in exact_trials:
            params = {k: v for k, v in exact_params.items() if v}
            try:
                r = requests.get(
                    "https://lrclib.net/api/get",
                    params=params,
                    headers=headers,
                    timeout=6,
                )
                if r.status_code == 200:
                    data = r.json()
                    synced = clean_lrc_metadata(data.get("syncedLyrics"))
                    plain = clean_lrc_metadata(data.get("plainLyrics"))
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
        expanded_queries = []
        if first_art:
            expanded_queries.append(f"{first_art} {clean_t}")
        if len(artist_tokens) >= 2:
            expanded_queries.append(f"{artist_tokens[0]} {artist_tokens[1]} {clean_t}")
        if f"{artist} {clean_t}" not in expanded_queries:
            expanded_queries.append(f"{artist} {clean_t}")
        if clean_t != title and f"{first_art} {title}" not in expanded_queries:
            expanded_queries.append(f"{first_art} {title}")
        for tok in artist_tokens:
            spaced = re.sub(r"\.([A-Za-zА-Яа-я])", r". \1", tok)
            for t_var in (tok, spaced):
                q_var = f"{t_var} {clean_t}"
                if q_var not in expanded_queries:
                    expanded_queries.append(q_var)

        for q in expanded_queries:
            try:
                r = requests.get(
                    "https://lrclib.net/api/search",
                    params={"q": q},
                    headers=headers,
                    timeout=8,
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        best_plain = None
                        target_clean = clean_track_title(title).lower()
                        for item in data:
                            api_title = item.get("trackName", "")
                            api_title_clean = clean_track_title(api_title).lower()

                            # Сверка названий трека
                            matched_title = (
                                target_clean == api_title_clean
                                or (target_clean in api_title_clean if len(target_clean) >= 4 else False)
                                or (api_title_clean in target_clean if len(api_title_clean) >= 4 else False)
                                or SequenceMatcher(None, target_clean, api_title_clean).ratio() >= 0.85
                            )
                            if not matched_title:
                                continue

                            synced = clean_lrc_metadata(item.get("syncedLyrics"))
                            plain = clean_lrc_metadata(item.get("plainLyrics"))
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
        """Ищет эталонный текст песни на Genius.com с ранжированием релевантности кандидатов."""
        headers = {"User-Agent": self.BROWSER_UA}

        def norm(s: str) -> str:
            return re.sub(r"[^\w]", "", (s or "").lower())

        clean_t = clean_track_title(title)
        artist_tokens = [a.strip() for a in re.split(r"[,;&]|\s+(?:feat\.?|ft\.?|x|and)\s+", artist, flags=re.I) if a.strip()]
        expanded_tokens = []
        for a in artist_tokens:
            if a not in expanded_tokens:
                expanded_tokens.append(a)
            spaced = re.sub(r"\.([A-Za-zА-Яа-я])", r". \1", a)
            if spaced != a and spaced not in expanded_tokens:
                expanded_tokens.append(spaced)

        queries: list[str] = []
        if expanded_tokens:
            queries.append(f"{expanded_tokens[0]} {clean_t}")
            if len(expanded_tokens) >= 2:
                queries.append(f"{expanded_tokens[0]} {expanded_tokens[1]} {clean_t}")
        queries.append(f"{artist} {clean_t}")
        if clean_t != title and expanded_tokens:
            queries.append(f"{expanded_tokens[0]} {title}")
        for tok in expanded_tokens:
            q_tok = f"{tok} {clean_t}"
            if q_tok not in queries:
                queries.append(q_tok)

        target_title_norm = norm(clean_t)
        clean_target_arts = [norm(a) for a in expanded_tokens if len(norm(a)) > 1]

        all_hits: list[dict] = []
        seen_urls = set()

        for q in queries:
            try:
                r = requests.get(
                    "https://genius.com/api/search/multi",
                    params={"q": q},
                    headers=headers,
                    timeout=8,
                )
                if r.status_code == 200:
                    sections = r.json().get("response", {}).get("sections", [])
                    found_exact_song = False
                    for s in sections:
                        for h in s.get("hits", []):
                            if h.get("type") == "song" or s.get("type") == "song":
                                res = h.get("result", {})
                                url = res.get("url")
                                if url and url not in seen_urls:
                                    seen_urls.add(url)
                                    h_title = res.get("title", "")
                                    h_art = res.get("artist_names", "") + " " + res.get("primary_artist", {}).get("name", "")
                                    if not is_translation(h_title, h_art):
                                        all_hits.append(res)
                                        if norm(clean_track_title(h_title)) == target_title_norm:
                                            found_exact_song = True
                    if found_exact_song and len(all_hits) >= 2:
                        break
            except Exception as e:
                log.debug("Genius search query '%s' error: %s", q, e)

        if not all_hits:
            return None

        # Ранжирование кандидатов
        best_hit = None
        best_score = -1

        for res in all_hits:
            hit_title = res.get("title", "")
            art_names = (res.get("artist_names", "") + " " + res.get("primary_artist", {}).get("name", "")).lower()

            if is_translation(hit_title, art_names):
                continue
            if "remix" in hit_title.lower() and "remix" not in title.lower():
                continue

            hit_title_clean = clean_track_title(hit_title)
            hit_title_norm = norm(hit_title_clean)
            if not hit_title_norm:
                continue

            score = 0
            # 1. Совпадение названия
            if hit_title_norm == target_title_norm:
                score += 60
                if hit_title_clean.strip().lower() == clean_t.strip().lower():
                    score += 20
            else:
                sim = SequenceMatcher(None, target_title_norm, hit_title_norm).ratio()
                if sim >= 0.8:
                    score += int(sim * 50)
                else:
                    continue  # Название слишком отличается

            # 2. Совпадение артистов (чем больше артистов релиза совпало, тем выше балл)
            cand_art_norm = norm(art_names)
            matched_arts = sum(1 for a in clean_target_arts if a in cand_art_norm)
            if not matched_arts:
                continue

            score += matched_arts * 30

            if score > best_score:
                best_score = score
                best_hit = res

        if not best_hit:
            return None

        song_url = best_hit.get("url")
        if not song_url:
            return None

        try:
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

            full_text = clean_lrc_metadata(full_text.strip())

            if full_text:
                return LyricResult(
                    text=full_text,
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

                    full_text = clean_lrc_metadata("\n".join(out))
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
                cleaned_lrc = clean_lrc_metadata(lrc_text)
                return LyricResult(
                    text=cleaned_lrc,
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

    def process_file(
        self,
        filepath: str | Path,
        overwrite: bool = False,
        smart_upgrade: bool = True,
    ) -> str:
        """Скачивает текст по водопаду и сохраняет .lrc файл рядом с треком.

        Args:
            filepath: путь к FLAC файлу.
            overwrite: если True, выполняет принудительный сетевой поиск и обновление.
            smart_upgrade: если True, защищает существующее караоке (🎤) от даунгрейда до plain (📄).

        Returns:
            Строку-статус для отчёта:
            "Синхронизированный [LRCLIB] 🎤" | "Обычный текст [Genius] 📄" | "Уже есть 📁" | "Не найдено ❌"
        """
        path = Path(filepath)
        if path.suffix.lower() != ".flac":
            return "Не аудиофайл"

        lrc_path = path.with_suffix(".lrc")
        existing_has_sync = False
        if lrc_path.exists():
            try:
                content = lrc_path.read_text(encoding="utf-8")
                existing_has_sync = has_timestamps(content)
            except Exception:
                pass

            # Если перезапись выключена:
            # - Если уже есть караоке -> пропускаем
            # - Если есть обычный текст, но smart_upgrade=False -> пропускаем
            if not overwrite and (existing_has_sync or not smart_upgrade):
                return "Уже есть 📁"

        try:
            audio = FLAC(str(path))
            arts = audio.get("artist", [])
            artist = ", ".join(arts) if arts else audio.get("albumartist", [""])[0]
            title = audio.get("title", [""])[0]
            album = audio.get("album", [""])[0]
            duration = int(audio.info.length) if hasattr(audio.info, "length") else 0

            if not artist or not title:
                return "Нет тегов ❌"

            res = self.find_lyrics(artist, title, album, duration)
            if res and res.text:
                # Защита от даунгрейда: если уже было караоке, а пришёл обычный текст — сохраняем караоке!
                if existing_has_sync and not res.is_synced:
                    return "Сохранено караоке 🎤 (в сети только обычный)"

                lrc_path.write_text(res.text, encoding="utf-8")
                try:
                    lrc_path.chmod(0o664)
                    os.utime(str(path), None)
                except Exception:
                    pass
                source_labels = {
                    "lrclib": "LRCLIB",
                    "lrcmux": "LRCMUX",
                    "syncedlyrics": "SyncedLyrics",
                    "genius": "Genius",
                    "manual": "Вручную",
                }
                source_label = source_labels.get(res.source, res.source.upper())

                prefix = "Обновлено караоке" if existing_has_sync else ("Синхронизированный" if res.is_synced else "Обычный текст")
                icon = "🎤" if res.is_synced else "📄"
                return f"{prefix} [{source_label}] {icon}"

            if existing_has_sync:
                return "Сохранено караоке 🎤"
            elif lrc_path.exists():
                return "Обычный текст 📄"

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

        audio_artist = ""
        audio_title = ""
        try:
            audio = FLAC(str(path))
            arts = audio.get("artist", [])
            audio_artist = ", ".join(arts) if arts else audio.get("albumartist", [""])[0]
            audio_title = audio.get("title", [""])[0]
        except Exception as e:
            log.debug("Ошибка получения тегов трека %s: %s", path, e)

        # 2. Если текста не было, скачиваем с Genius/водопада
        if not plain_text:
            if audio_artist and audio_title:
                res = self._fetch_genius(audio_artist, audio_title) or self._fetch_lrcmux(audio_artist, audio_title)
                if res and res.text:
                    plain_text = res.text

        if not plain_text.strip():
            return None, "Текст песни не найден ни в файле, ни на Genius"

        # 3. Выравнивание через faster-whisper + lyric-align
        try:
            from aligner import align_lyrics
            synced_text = align_lyrics(path, plain_text, model=model)

            # Если выравнивание локального текста из .lrc не сошлось (текст чужой),
            # пробуем перезапросить свежий эталонный текст с Genius
            if (not synced_text or not has_timestamps(synced_text)) and audio_artist and audio_title:
                log.info("Локальный текст .lrc не совпал с аудио. Пробуем перекачать с Genius: %s — %s", audio_artist, audio_title)
                fresh_res = self._fetch_genius(audio_artist, audio_title) or self._fetch_lrcmux(audio_artist, audio_title)
                if fresh_res and fresh_res.text and fresh_res.text.strip() != plain_text.strip():
                    log.info("Найден альтернативный текст на Genius. Повторный запуск выравнивания...")
                    retry_synced = align_lyrics(path, fresh_res.text, model=model)
                    if retry_synced and has_timestamps(retry_synced):
                        synced_text = retry_synced

            if synced_text and has_timestamps(synced_text):
                lrc_path.write_text(synced_text, encoding="utf-8")
                try:
                    lrc_path.chmod(0o664)
                    os.utime(str(path), None)
                except Exception:
                    pass
                res = LyricResult(
                    text=synced_text,
                    is_synced=True,
                    source="ai",
                    track_title=path.stem,
                )
                return res, "Таймкоды успешно расставлены нейросетью ✨"
            return None, "Нейросети не удалось сопоставить слова с аудио (текст не совпадает с песней)"
        except Exception as e:
            log.error("Ошибка при AI-выравнивании %s: %s", path.name, e)
            return None, f"Ошибка нейросети: {e}"

    def save_manual_lyrics(self, lrc_path: Path, text: str) -> LyricResult:
        """Сохраняет вручную введённый текст в файл .lrc."""
        text = text.strip()
        is_sync = has_timestamps(text)
        lrc_path.write_text(text, encoding="utf-8")
        try:
            lrc_path.chmod(0o664)
            flac_path = lrc_path.with_suffix(".flac")
            if flac_path.exists():
                os.utime(str(flac_path), None)
        except Exception:
            pass
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
