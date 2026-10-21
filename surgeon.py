"""Вшивание обложек, очистка тегов и синхронизация метаданных FLAC (ADR-0006).

Выполняет хирургическую обработку аудиофайлов:
  - Whitelist фильтрация разрешённых тегов и удаление мусорных полей.
  - Извлечение фитов (feat./ft.) из названия трека и добавление их в тег ARTIST.
  - Санитизация имён артистов через TagSanitizer.
  - Вшивание cover.jpg в FLAC-контейнер.
  - Синхронизация полей DATE и YEAR.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from mutagen.flac import FLAC, Picture

from sanitizer import TagSanitizer

log = logging.getLogger(__name__)


class FLACSurgeon:
    """Очистка метаданных и вшивание обложек в FLAC-файлы."""

    # Tag Whitelist по ADR-0006
    WHITELIST: frozenset[str] = frozenset({
        "title", "artist", "albumartist", "album",
        "date", "year", "originaldate", "tracknumber", "tracktotal",
        "discnumber", "disctotal", "genre", "label",
        "rating", "compilation",
        "replaygain_track_gain", "replaygain_track_peak",
        "replaygain_album_gain", "replaygain_album_peak",
        "replaygain_reference_loudness",
    })

    def __init__(self, sanitizer: TagSanitizer | None = None) -> None:
        self.sanitizer = sanitizer if sanitizer is not None else TagSanitizer()

    def _embed_cover_from_file(
        self,
        audio: FLAC,
        folder_path: str | Path,
        force_update: bool = False,
    ) -> None:
        """Вшивает обложку из локального cover.jpg, если он существует (ADR-0012)."""
        if force_update:
            audio.clear_pictures()

        if audio.pictures:
            return

        cover_path = Path(folder_path) / "cover.jpg"
        if not cover_path.exists():
            return

        try:
            img_data = cover_path.read_bytes()
            if not img_data:
                return

            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Front Cover"
            pic.data = img_data
            audio.add_picture(pic)
            log.debug("Обложка вшита из %s", cover_path.name)
        except OSError as e:
            log.warning("Не удалось прочитать обложку %s: %s", cover_path, e)

    def heal_file(self, filepath: str | Path, force_update: bool = False) -> None:
        """Главный конвейер очистки метаданных FLAC-файла.

        Args:
            filepath: Путь к обрабатываемому .flac файлу.
            force_update: Перезаписывать ли обложку, даже если она уже вшита.
        """
        path = Path(filepath)
        if path.suffix.lower() != ".flac":
            return

        try:
            audio = FLAC(str(path))
        except Exception as e:
            log.error("Файл повреждён или не читается: %s (%s)", path.name, e)
            return

        # 0. Нормализация тегов лейбла (ORGANIZATION / PUBLISHER -> LABEL)
        if "label" not in audio:
            for alt in ("organization", "publisher", "recordlabel", "record_label"):
                if alt in audio:
                    audio["label"] = audio[alt]
                    break

        # 1. Фильтрация по whitelist тегов и очистка мусорных значений
        saved_tags: dict[str, list[str]] = {}
        for key in list(audio.keys()):
            lower_key = key.lower()
            if lower_key in self.WHITELIST:
                cleaned_vals: list[str] = []
                for v in audio[key]:
                    v_str = str(v).strip()
                    if not v_str:
                        continue
                    if lower_key in ("year", "date", "originaldate") and v_str in ("0", "0000"):
                        continue
                    if lower_key in ("tracktotal", "disctotal") and v_str == "0":
                        continue
                    if lower_key == "discnumber" and v_str == "0":
                        v_str = "1"
                    if lower_key == "label" and v_str.lower() in ("[no label]", "no label"):
                        continue
                    cleaned_vals.append(v_str)

                if cleaned_vals:
                    saved_tags[lower_key] = cleaned_vals

            del audio[key]

        # 2. Восстановление очищенных тегов
        for key, val in saved_tags.items():
            audio[key] = val

        # 3. Извлечение фитов из названия и санитизация артистов
        extracted_feats = ""
        if "title" in audio:
            title_str = audio["title"][0]

            feat_pattern = re.compile(
                r"(?i)(?:[\[\(](?:feat\.?|ft\.?)\s+([^\]\)]+)[\]\)]|\s+(?:feat\.?|ft\.?)\s+(.+)$)"
            )
            feat_match = feat_pattern.search(title_str)
            if feat_match:
                extracted_feats = feat_match.group(1) or feat_match.group(2)
                title_str = feat_pattern.sub("", title_str)

            prod_pattern = re.compile(r"(?i)[\[\(]prod\..*?[\]\)]")
            title_str = prod_pattern.sub("", title_str)
            audio["title"] = [re.sub(r"\s+", " ", title_str).strip()]

        if "artist" in audio:
            raw = " & ".join(audio["artist"])
            if extracted_feats:
                raw += f" & {extracted_feats}"
            audio["artist"] = self.sanitizer.process_artists(raw)

        if "albumartist" in audio:
            raw = " & ".join(audio["albumartist"])
            audio["albumartist"] = self.sanitizer.process_artists(raw)

        # 4. Вшивание обложки из cover.jpg
        self._embed_cover_from_file(audio, path.parent, force_update)

        # 5. Синхронизация DATE и YEAR
        if "date" in audio and "year" not in audio:
            audio["year"] = [str(audio["date"][0])[:4]]
        elif "year" in audio and "date" not in audio:
            audio["date"] = audio["year"]

        # 6. Сохранение изменений
        audio.save()
        log.debug("Файл очищен: %s", path.name)
