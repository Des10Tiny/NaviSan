"""Управление именами файлов и папок релиза (ADR-0007).

Приводит структуру файлов к единообразному формату Navidrome:
  - Имена треков: "{track_number}. {safe_title}.flac" (с дополнением нулями).
  - Имя папки: "{Artist} - {Album}".
  - Защита от недопустимых символов файловой системы (/:*?"<>|).
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter
from pathlib import Path

from mutagen.flac import FLAC

log = logging.getLogger(__name__)


class FileSystemManager:
    """Нормализация имён треков и папок альбомов на файловой системе."""

    INVALID_CHARS = re.compile(r'[\\/*?:"<>|]')

    def clean_name(self, name: str) -> str:
        """Удаляет запрещённые символы файловой системы и лишние пробелы."""
        return self.INVALID_CHARS.sub("", name).strip()

    def rename_file(self, filepath: str | Path) -> str:
        """Переименовывает трек в формат '01. Title.flac'.

        Args:
            filepath: Текущий путь к FLAC-файлу.

        Returns:
            Новый (или неизменённый) строковый путь к файлу.
        """
        path = Path(filepath)
        if not path.exists() or path.suffix.lower() != ".flac":
            return str(filepath)

        try:
            audio = FLAC(str(path))
        except Exception as e:
            log.warning("Не удалось прочитать теги для переименования %s: %e", path.name, e)
            return str(filepath)

        track_raw = audio.get("tracknumber", [""])[0].split("/")[0].strip()
        track_num = track_raw.zfill(2) if track_raw.isdigit() else ""

        title = audio.get("title", ["Unknown_Title"])[0]
        safe_title = self.clean_name(title)

        new_filename = f"{track_num}. {safe_title}.flac" if track_num else f"{safe_title}.flac"
        new_path = path.parent / new_filename

        if path != new_path:
            path.rename(new_path)
            log.debug("Файл переименован: %s -> %s", path.name, new_filename)
            return str(new_path)

        return str(filepath)

    def rename_album_folder(self, folder_path: str | Path) -> str:
        """Анализирует все FLAC-треки в папке и переименовывает её в 'Artist - Album'.

        Args:
            folder_path: Путь к папке альбома.

        Returns:
            Новый (или неизменённый) строковый путь к папке.
        """
        folder = Path(folder_path)
        flac_files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() == ".flac"]
        if not flac_files:
            return str(folder_path)

        album_artists_list: list[tuple[str, ...]] = []
        track_artists_sets: list[set[str]] = []
        album_names: list[str] = []

        # 1. Сбор метаданных по всем трекам альбома
        for f in flac_files:
            try:
                audio = FLAC(str(f))
            except Exception:
                continue

            aa = audio.get("albumartist", [])
            if aa:
                album_artists_list.append(tuple(aa))

            a = audio.get("artist", [])
            if a:
                track_artists_sets.append(set(a))

            al = audio.get("album", audio.get("title", ["Unknown_Release"]))
            album_names.append(al[0])

        # 2. Определение канонического названия альбома
        best_album = Counter(album_names).most_common(1)[0][0] if album_names else "Unknown_Release"

        # 3. Определение артиста(ов) релиза
        if album_artists_list:
            best_aa = Counter(album_artists_list).most_common(1)[0][0]
            final_artists = list(best_aa)
        elif track_artists_sets:
            common = set.intersection(*track_artists_sets)
            if common:
                try:
                    first_track_artists = FLAC(str(flac_files[0])).get("artist", [])
                    final_artists = [art for art in first_track_artists if art in common]
                except Exception:
                    final_artists = list(common)
            else:
                final_artists = ["Various Artists"]
        else:
            final_artists = ["Unknown Artist"]

        # 4. Формирование нового имени папки
        artists_str = ", ".join(final_artists)
        new_dirname = self.clean_name(f"{artists_str} - {best_album}")
        new_folder = folder.parent / new_dirname

        # 5. Проверка необходимости переименования
        if folder == new_folder:
            return str(folder_path)

        # Защита от дурака при изменении только регистра
        if folder.name.lower() == new_folder.name.lower():
            temp_path = folder.parent / f"{folder.name}_temp_rename"
            folder.rename(temp_path)
            temp_path.rename(new_folder)
            log.info("Папка переименована (регистр): %s", new_dirname)
            return str(new_folder)

        # Если целевая папка уже существует — не затираем её
        if new_folder.exists():
            log.warning("Папка с именем '%s' уже существует рядом. Пропускаю переименование.", new_dirname)
            return str(folder_path)

        folder.rename(new_folder)
        log.info("Папка переименована: %s", new_dirname)
        return str(new_folder)
