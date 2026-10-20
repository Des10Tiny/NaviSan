"""Нормализация имён артистов через базу Navidrome и онлайн-каталоги (Deezer/iTunes).

Разбивает составные строки исполнителей (feat, ft, x, and, запятые), сверяет их с существующей
базой данных Navidrome (navidrome.db) и осуществляет безопасную автокоррекцию регистра
и спецсимволов через проверенные онлайн-каталоги (Deezer, iTunes) с локальным кешированием.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


def is_safe_match(query: str, candidate: str) -> bool:
    """Проверяет безопасность замены артиста через fuzzy matching.

    Не допускает подмены на других артистов (например, Ghost -> Ghostface Killah),
    но разрешает нормализацию регистра, спецсимволов ($ -> s, & -> and) и пунктуации.
    """
    def norm(s: str) -> str:
        s = s.lower().replace("$", "s").replace("&", "and")
        return re.sub(r"[^\w]", "", s)

    nq = norm(query)
    nc = norm(candidate)

    if not nq or not nc:
        return False

    if nq == nc:
        return True

    return SequenceMatcher(None, nq, nc).ratio() >= 0.85


def pick_best_candidate(query: str, candidates: list[str]) -> str | None:
    """Выбирает лучшего кандидата: точное совпадение (без учёта регистра) имеет высший приоритет,
    затем безопасное fuzzy/символьное совпадение."""
    q_lower = query.strip().lower()
    for cand in candidates:
        if cand.strip().lower() == q_lower:
            return cand.strip()
    for cand in candidates:
        if is_safe_match(query, cand):
            return cand.strip()
    return None


class TagSanitizer:
    """Санитизация и приведение имён артистов к единому стандарту."""

    def __init__(
        self,
        db_path: str | Path = "navidrome.db",
        cache_path: str | Path = "artists_cache.json",
    ) -> None:
        self.db_path = str(db_path)
        self.cache_path = str(cache_path)

        self.artists_cache: dict[str, str | None] = self._load_cache()
        self.unmapped_artists: set[str] = set()
        self.split_pattern = re.compile(
            r"(?i)\s+(?:feat\.?|ft\.?|x|and|и)\s+|\s*[,;&]\s*"
        )

    def _load_cache(self) -> dict[str, str | None]:
        """Загружает локальный кеш онлайн-проверок имён артистов."""
        if not os.path.exists(self.cache_path):
            return {}
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {k.lower(): (str(v) if v is not None else None) for k, v in data.items()}
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Ошибка чтения кеша артистов %s: %s", self.cache_path, e)
            return {}

    def _save_cache_entry(self, wrong_name: str, canonical_name: str | None) -> None:
        """Сохраняет результат проверки артиста в локальный кеш."""
        self.artists_cache[wrong_name.lower()] = canonical_name
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self.artists_cache, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log.warning("Ошибка сохранения кеша артистов %s: %s", self.cache_path, e)

    def _fetch_online_canonical(self, artist_name: str) -> str | None:
        """Ищет каноническое написание артиста в проверенных каталогах (Deezer, iTunes, Genius)."""
        name_clean = artist_name.strip()
        if not name_clean:
            return None

        # 1. Deezer API (официальный каталог лейблов, быстрый и точный)
        try:
            r = httpx.get(
                "https://api.deezer.com/search/artist",
                params={"q": name_clean},
                timeout=4.0,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                cands = [item["name"] for item in data[:5] if item.get("name")]
                best = pick_best_candidate(name_clean, cands)
                if best:
                    return best
        except Exception as e:
            log.debug("Deezer lookup failed for '%s': %s", name_clean, e)

        # 2. iTunes Search API (надёжный fallback без ключей с отличной поддержкой кириллицы)
        try:
            r = httpx.get(
                "https://itunes.apple.com/search",
                params={"term": name_clean, "entity": "musicArtist", "limit": "5"},
                timeout=4.0,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code == 200:
                results = r.json().get("results", [])
                cands = [item["artistName"] for item in results[:5] if item.get("artistName")]
                best = pick_best_candidate(name_clean, cands)
                if best:
                    return best
        except Exception as e:
            log.debug("iTunes lookup failed for '%s': %s", name_clean, e)

        # 3. Genius API (если задан токен в переменных окружения)
        token = os.getenv("GENIUS_ACCESS_TOKEN") or os.getenv("GENIUS_TOKEN")
        if token:
            try:
                r = httpx.get(
                    "https://api.genius.com/search",
                    params={"q": name_clean},
                    headers={"Authorization": f"Bearer {token}", "User-Agent": "Navisan/1.0"},
                    timeout=4.0,
                )
                if r.status_code == 200:
                    hits = r.json().get("response", {}).get("hits", [])
                    cands = [
                        hit.get("result", {}).get("primary_artist", {}).get("name")
                        for hit in hits[:5]
                        if hit.get("result", {}).get("primary_artist", {}).get("name")
                    ]
                    best = pick_best_candidate(name_clean, cands)
                    if best:
                        return best
            except Exception as e:
                log.debug("Genius lookup failed for '%s': %s", name_clean, e)

        return None

    def _get_artist_from_db(self, artist_name: str) -> str | None:
        """Проверяет наличие артиста в базе данных Navidrome (регистронезависимо)."""
        if not os.path.exists(self.db_path):
            return None

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name FROM artist WHERE name COLLATE NOCASE = ?",
                    (artist_name,),
                )
                result = cursor.fetchone()
                if result:
                    return str(result[0])
        except sqlite3.Error as e:
            log.debug("Ошибка чтения базы Navidrome: %s", e)

        return None

    def _get_compound_artists_from_db(self) -> set[str]:
        """Возвращает из navidrome.db имена артистов, содержащие разделители (, & and)."""
        if not os.path.exists(self.db_path):
            return set()
        res = set()
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name FROM artist WHERE name LIKE '%,%' OR name LIKE '%&%' OR name LIKE '% and %'"
                )
                for r in cursor.fetchall():
                    val = str(r[0]).strip()
                    if val and len(val) < 80:
                        res.add(val.lower())
        except sqlite3.Error as e:
            log.debug("Ошибка чтения составных артистов из Navidrome: %s", e)
        return res

    def process_artists(self, raw_artist_string: str) -> list[str]:
        """Разбивает строку артистов и приводит каждое имя к каноническому виду."""
        raw_str = (raw_artist_string or "").strip()
        if not raw_str:
            return []

        # 1. Быстрая проверка: полное совпадение с кешем или базой Navidrome
        raw_lower = raw_str.lower()
        if raw_lower in self.artists_cache and self.artists_cache[raw_lower]:
            return [self.artists_cache[raw_lower]]
        db_match = self._get_artist_from_db(raw_str)
        if db_match and not db_match.islower() and not (db_match.isupper() and len(db_match) > 3):
            return [db_match]

        # 2. Формируем список составных артистов из navidrome.db
        protected = self._get_compound_artists_from_db()

        # 3. Экранирование защищённых групп токенами перед сплитом
        sorted_prot = sorted(protected, key=len, reverse=True)
        tokens: dict[str, str] = {}
        masked = raw_str

        for p in sorted_prot:
            pattern = re.compile(r"(?<!\w)" + re.escape(p) + r"(?!\w)", re.IGNORECASE)
            def repl(m: re.Match) -> str:
                tok = f"\x00PROT_{len(tokens)}\x00"
                tokens[tok] = m.group(0)
                return tok
            masked = pattern.sub(repl, masked)

        # 4. Разделение по разделителям
        split_artists = self.split_pattern.split(masked)
        clean_artists: list[str] = []

        for artist in split_artists:
            artist = artist.strip()
            if not artist:
                continue

            # Восстанавливаем защищённые токены
            for tok, orig in tokens.items():
                if tok in artist:
                    artist = artist.replace(tok, orig)

            artist_lower = artist.lower()

            # 1. Приоритет: результат из локального кеша онлайн-проверок
            if artist_lower in self.artists_cache:
                cached = self.artists_cache[artist_lower]
                final_artist = cached if cached else artist
            else:
                # 2. Проверка локальной базы Navidrome
                db_artist = self._get_artist_from_db(artist)
                if db_artist and not db_artist.islower() and not (db_artist.isupper() and len(db_artist) > 3):
                    final_artist = db_artist
                else:
                    # 3. Онлайн-каноникализация через Deezer / iTunes с проверкой безопасности
                    online_name = self._fetch_online_canonical(artist)
                    if online_name:
                        self._save_cache_entry(artist_lower, online_name)
                        final_artist = online_name
                        log.info("Онлайн-нормализация артиста: '%s' -> '%s'", artist, online_name)
                    elif db_artist:
                        self._save_cache_entry(artist_lower, db_artist)
                        final_artist = db_artist
                    else:
                        # Записываем None в кеш, чтобы не повторять сетевой запрос
                        self._save_cache_entry(artist_lower, None)
                        final_artist = artist
                        self.unmapped_artists.add(artist)

            clean_artists.append(final_artist)

        return list(dict.fromkeys(clean_artists))

    def export_unmapped_artists(self, filepath: str | Path = "unknown_artists.txt") -> None:
        """Сохраняет список неизвестных артистов в текстовый файл для ручного аудита."""
        filepath_str = str(filepath)
        if not self.unmapped_artists:
            if os.path.exists(filepath_str):
                try:
                    os.remove(filepath_str)
                except OSError:
                    pass
            return

        with open(filepath_str, "w", encoding="utf-8") as f:
            for artist in sorted(self.unmapped_artists):
                f.write(f"{artist}\n")
        log.info("Неизвестные артисты записаны в '%s'", filepath_str)
