"""Нормализация имён артистов через таблицу алиасов и базу Navidrome (ADR-0005).

Разбивает составные строки исполнителей (feat, ft, x, and, запятые),
приводит имена к каноническому виду по словарю aliases.json и сверяет
их с существующей базой данных Navidrome (navidrome.db).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)


class TagSanitizer:
    """Санитизация и приведение имён артистов к единому стандарту."""

    def __init__(
        self,
        aliases_path: str | Path = "aliases.json",
        db_path: str | Path = "navidrome.db",
    ) -> None:
        self.aliases_path = str(aliases_path)
        self.db_path = str(db_path)

        self.canonical_names_lower: set[str] = set()
        self.aliases: dict[str, str] = self._load_aliases()
        self.unmapped_artists: set[str] = set()
        self.split_pattern = re.compile(
            r"(?i)\s+(?:feat\.?|ft\.?|x|and|и)\s+|\s*[,;&]\s*"
        )

    def _load_aliases(self) -> dict[str, str]:
        """Загружает словарь алиасов. Все ключи приводятся к нижнему регистру."""
        if not os.path.exists(self.aliases_path):
            with open(self.aliases_path, "w", encoding="utf-8") as f:
                json.dump({}, f)
            return {}

        try:
            with open(self.aliases_path, "r", encoding="utf-8") as f:
                raw_aliases = json.load(f)

            aliases = {k.lower(): str(v) for k, v in raw_aliases.items()}
            self.canonical_names_lower = {v.lower() for v in aliases.values()}
            return aliases
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Ошибка чтения файла алиасов %s: %s", self.aliases_path, e)
            return {}

    def save_alias(self, wrong_name: str, correct_name: str) -> None:
        """Сохраняет новый алиас в файл."""
        self.aliases[wrong_name.lower()] = correct_name
        self.canonical_names_lower.add(correct_name.lower())
        with open(self.aliases_path, "w", encoding="utf-8") as f:
            json.dump(self.aliases, f, ensure_ascii=False, indent=4)

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

    def process_artists(self, raw_artist_string: str) -> list[str]:
        """Разбивает строку артистов и приводит каждое имя к каноническому виду.

        Args:
            raw_artist_string: Исходная строка из тега ARTIST (например, "A feat. B & C").

        Returns:
            Список очищенных уникальных имён артистов в порядке появления.
        """
        split_artists = self.split_pattern.split(raw_artist_string)
        clean_artists: list[str] = []

        for artist in split_artists:
            artist = artist.strip()
            if not artist:
                continue

            artist_lower = artist.lower()

            if artist_lower in self.aliases:
                canonical = self.aliases[artist_lower]
            elif artist_lower in self.canonical_names_lower:
                canonical = artist
            else:
                canonical = artist
                self.unmapped_artists.add(artist)

            # Сверяем с базой Navidrome
            db_artist = self._get_artist_from_db(canonical)
            if db_artist:
                final_artist = db_artist
                self.unmapped_artists.discard(artist)
            else:
                final_artist = canonical

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
