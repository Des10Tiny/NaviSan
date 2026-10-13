import re
import json
import os
import sqlite3

class TagSanitizer:
    def __init__(self, aliases_path="aliases.json", db_path="navidrome.db"):
        self.aliases_path = aliases_path
        self.db_path = db_path
        
        self.canonical_names_lower = set()
        self.aliases = self._load_aliases()
        self.unmapped_artists = set() 
        self.split_pattern = re.compile(r'(?i)\s+(?:feat\.?|ft\.?|x|and|и)\s+|\s*[,;&]\s*')

    def _load_aliases(self):
        if not os.path.exists(self.aliases_path):
            with open(self.aliases_path, 'w', encoding='utf-8') as f:
                json.dump({}, f)
            return {}
            
        with open(self.aliases_path, 'r', encoding='utf-8') as f:
            raw_aliases = json.load(f)
            
            # ФИКС 1: Принудительно делаем все КЛЮЧИ словаря маленькими буквами
            # Теперь неважно, как ты напишешь в json: "Ye", "YE" или "ye"
            aliases = {k.lower(): v for k, v in raw_aliases.items()}
            
            # Собираем эталонные значения
            self.canonical_names_lower = {str(v).lower() for v in aliases.values()}
            return aliases

    def save_alias(self, wrong_name, correct_name):
        self.aliases[wrong_name.lower()] = correct_name
        self.canonical_names_lower.add(correct_name.lower())
        with open(self.aliases_path, 'w', encoding='utf-8') as f:
            json.dump(self.aliases, f, ensure_ascii=False, indent=4)

    def _get_artist_from_db(self, artist_name):
        """Возвращает имя из БД, либо None, если артист не найден."""
        if not os.path.exists(self.db_path):
            return None

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM artist WHERE name COLLATE NOCASE = ?", (artist_name,))
                result = cursor.fetchone()
                if result:
                    return result[0] # Артист найден!
        except sqlite3.Error as e:
            print(f"⚠️ Ошибка чтения базы Navidrome: {e}")
            
        return None # Артист не найден

    def process_artists(self, raw_artist_string):
        """Полный пайплайн обработки артистов."""
        split_artists = self.split_pattern.split(raw_artist_string)
        clean_artists = []
        
        for artist in split_artists:
            artist = artist.strip()
            if not artist:
                continue
                
            artist_lower = artist.lower()
            
            # 1. Если это кривое имя (есть в левой части словаря)
            if artist_lower in self.aliases:
                canonical_artist = self.aliases[artist_lower]
                
            # 2. Если это уже идеальное имя (есть в правой части словаря)
            elif artist_lower in self.canonical_names_lower:
                canonical_artist = artist
                
            # 3. Имя пока вообще нигде не встречается
            else:
                canonical_artist = artist
                self.unmapped_artists.add(artist)
            
            # Сверяем с базой Navidrome
            db_artist = self._get_artist_from_db(canonical_artist)
            
            # ФИКС 2: Если база отдала имя, значит она 100% его знает!
            if db_artist:
                final_artist = db_artist
                # Безжалостно удаляем из списка неизвестных
                if artist in self.unmapped_artists:
                    self.unmapped_artists.remove(artist)
            else:
                final_artist = canonical_artist
            
            clean_artists.append(final_artist)
            
        return list(dict.fromkeys(clean_artists))

    def export_unmapped_artists(self, filepath="unknown_artists.txt"):
        """Выгружает всех незнакомых артистов в текстовый файл."""
        if not self.unmapped_artists:
            if os.path.exists(filepath):
                os.remove(filepath) # Удаляем старый файл, если все артисты теперь знакомы
            return
            
        with open(filepath, "w", encoding="utf-8") as f:
            for artist in sorted(self.unmapped_artists):
                f.write(f"{artist}\n")
        print(f"\n📝 Внимание: Есть новые артисты. Они записаны в '{filepath}'.")