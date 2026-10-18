import os
from mutagen.flac import FLAC, Picture
from sanitizer import TagSanitizer
import re

class FLACSurgeon:
    def __init__(self, sanitizer):
        self.sanitizer = sanitizer
        
        # Tag Whitelist по ADR-0006
        self.whitelist = {
            'title', 'artist', 'albumartist', 'album',
            'date', 'year', 'originaldate', 'tracknumber', 'tracktotal',
            'discnumber', 'disctotal', 'genre', 'label',
            'rating', 'compilation',
            'replaygain_track_gain', 'replaygain_track_peak',
            'replaygain_album_gain', 'replaygain_album_peak',
        }

    def _embed_cover_from_file(self, audio: FLAC, folder_path: str, force_update: bool = False) -> None:
        """Вшивает обложку из локального cover.jpg, если он существует.

        Скачивание обложки — задача Telegram-бота (ADR-0012). Этот метод только
        читает уже готовый файл и записывает его в FLAC-контейнер.
        """
        if force_update:
            audio.clear_pictures()

        if audio.pictures:
            return  # обложка уже вшита, не трогаем

        cover_path = os.path.join(folder_path, "cover.jpg")
        if not os.path.exists(cover_path):
            return

        with open(cover_path, "rb") as f:
            img_data = f.read()

        if not img_data:
            return

        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Front Cover"
        pic.data = img_data
        audio.add_picture(pic)
        print(f"📁 Обложка вшита из cover.jpg")

    def heal_file(self, filepath: str, force_update: bool = False) -> None:
        """Главный конвейер очистки файла."""
        if not filepath.lower().endswith(".flac"):
            return

        try:
            audio = FLAC(filepath)
        except Exception as e:
            print(f"❌ Ошибка чтения (файл повреждён): {os.path.basename(filepath)}: {e}")
            return

        # 1. ЧИСТКА ОТ МУСОРА И БИТЫХ ЗНАЧЕНИЙ
        saved_tags: dict[str, list[str]] = {}
        for key in list(audio.keys()):
            lower_key = key.lower()
            if lower_key in self.whitelist:
                cleaned_vals = []
                for v in audio[key]:
                    v_str = str(v).strip()

                    if not v_str:
                        continue
                    if lower_key in ("year", "date", "originaldate") and v_str in ("0", "0000"):
                        continue
                    if lower_key in ("tracktotal", "disctotal") and v_str == "0":
                        continue
                    if lower_key == "discnumber" and v_str == "0":
                        v_str = "1"  # нулевых дисков не бывает
                    if lower_key == "label" and v_str.lower() in ("[no label]", "no label"):
                        continue

                    cleaned_vals.append(v_str)

                if cleaned_vals:
                    saved_tags[lower_key] = cleaned_vals

            del audio[key]

        # 2. ВОССТАНОВЛЕНИЕ ИДЕАЛЬНЫХ ТЕГОВ
        for key, val in saved_tags.items():
            audio[key] = val

        # 3. ОБРАБОТКА ИМЁН АРТИСТОВ И ОЧИСТКА НАЗВАНИЯ ТРЕКА
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

        # 4. ОБЛОЖКА ИЗ ЛОКАЛЬНОГО ФАЙЛА
        # Скачивание — задача бота (ADR-0012). Здесь только вшиваем готовый cover.jpg.
        folder_path = os.path.dirname(filepath)
        self._embed_cover_from_file(audio, folder_path, force_update)

        # 5. ФИНАЛЬНАЯ СИНХРОНИЗАЦИЯ ДАТ
        # DATE и YEAR должны быть согласованы: некоторые плееры читают только один из них.
        if "date" in audio and "year" not in audio:
            audio["year"] = [str(audio["date"][0])[:4]]
        elif "year" in audio and "date" not in audio:
            audio["date"] = audio["year"]

        # 6. СОХРАНЕНИЕ
        audio.save()
        print(f"✨ Готово: {os.path.basename(filepath)}")