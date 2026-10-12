import re
import json
import os
import sqlite3

class TagSanitizer:
    def __init__(self, aliases_path="aliases.json", db_path="navidrome.db"):
        self.aliases_path = aliases_path
        self.db_path = db_path
        self.aliases = self._load_aliases()

        # (?i) - игнорировать регистр (поймает и And, и AND)
        # \s+(?:feat\.?|ft\.?|x|and|и)\s+ - слова-разделители строго между пробелами
        # |\s*[,;&]\s* - символы-разделители (пробелы вокруг них опциональны)

        self.split_pattern = re.compile(r'(?i)\s+(?:feat\.?|ft\.?|x|and|и)\s+|\s*[,;&]\s*')

    def _load_aliases(self):
        if not os.path.exists(self.aliases_path):
            with open(self.aliases_path, 'w', encoding='utf-8') as f:
                json.dump({}, f)
            return {}
        with open(self.aliases_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def save_alias(self, wrong_name, correct_name):
        self.aliases[wrong_name] = correct_name
        with open(self.aliases_path, 'w', encoding='utf-8') as f:
            json.dump(self.aliases, f, ensure_ascii=False, indent=4)

    def _get_artist_from_db(self, artist_name):
        """
        Ищет артиста в базе Navidrome без учета регистра.
        Возвращает каноничное имя из БД, если найдено, иначе возвращает исходную строку.
        """
        if not os.path.exists(self.db_path):
            return artist_name

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM artist WHERE name COLLATE NOCASE = ?", (artist_name,))
                result = cursor.fetchone()
                if result:
                    return result[0]
                
        except sqlite3.Error as e:
            print(f"⚠️ Ошибка чтения базы Navidrome: {e}")
            
        return artist_name

    def process_artists(self, raw_artist_string):
        """Полный пайплайн обработки артистов."""
        split_artists = self.split_pattern.split(raw_artist_string)
        clean_artists = []
        
        for artist in split_artists:
            artist = artist.strip()
            if not artist:
                continue
                
            # 1. Применяем словарь алиасов
            canonical_artist = self.aliases.get(artist, artist)
            
            # 2. Сверяем с базой Navidrome для унификации регистра
            final_artist = self._get_artist_from_db(canonical_artist)
            
            clean_artists.append(final_artist)
            
        return clean_artists