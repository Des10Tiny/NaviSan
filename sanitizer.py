"""Нормализация имён артистов через базу Navidrome.

Разбивает составные строки исполнителей (feat, ft, x, and, запятые), сверяет их с существующей
базой данных Navidrome (navidrome.db) для сохранения единообразия регистра уже имеющихся артистов.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)


VARIOUS_ARTISTS_NAMES: frozenset[str] = frozenset({
    "various artists",
    "various",
    "va",
    "v/a",
    "v.a.",
    "soundtrack",
    "ost",
})


class TagSanitizer:
    """Санитизация и приведение имён артистов к единому стандарту."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        cache_path: str | Path | None = None,
    ) -> None:
        if db_path is None:
            db_path = os.environ.get("NAVISAN_DB", "navidrome.db")
        self.db_path = str(db_path)
        self.split_pattern = re.compile(
            r"(?i)\s+(?:feat\.?|ft\.?|x|and|и)\s+|\s*[,;&]\s*"
        )

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

        raw_lower = raw_str.lower()
        if raw_lower in VARIOUS_ARTISTS_NAMES:
            return ["Various Artists"]

        # 1. Быстрая проверка: полное совпадение с базой Navidrome
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
            if artist_lower in VARIOUS_ARTISTS_NAMES:
                clean_artists.append("Various Artists")
                continue

            # Сверка с локальной базой Navidrome (регистронезависимо)
            db_artist = self._get_artist_from_db(artist)
            if db_artist and not db_artist.islower() and not (db_artist.isupper() and len(db_artist) > 3):
                final_artist = db_artist
            else:
                final_artist = artist

            clean_artists.append(final_artist)

        return list(dict.fromkeys(clean_artists))
