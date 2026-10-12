import os
import re
from collections import Counter
from mutagen.flac import FLAC

class FileSystemManager:
    def __init__(self):
        # Запрещенные символы для имен файлов (Windows/Linux)
        self.invalid_chars = re.compile(r'[\\/*?:"<>|]')

    def clean_name(self, name):
        """Удаляет запрещенные символы из строки"""
        return self.invalid_chars.sub("", name).strip()

    def rename_file(self, filepath):
        """Переименовывает файл в 'Номер. Название.flac'"""
        if not os.path.exists(filepath):
            return filepath
            
        audio = FLAC(filepath)
        dir_name = os.path.dirname(filepath)
        
        track_num = audio.get('tracknumber', [''])[0].split('/')[0]
        if track_num.isdigit():
            track_num = track_num.zfill(2)
            
        title = audio.get('title', ['Unknown_Title'])[0]
        safe_title = self.clean_name(title)
        
        if track_num:
            new_filename = f"{track_num}. {safe_title}.flac"
        else:
            new_filename = f"{safe_title}.flac"
            
        new_filepath = os.path.join(dir_name, new_filename)
        
        if filepath != new_filepath:
            os.rename(filepath, new_filepath)
            print(f"   ✏️ Файл переименован: {new_filename}")
            return new_filepath
            
        return filepath

    def rename_album_folder(self, folder_path):
        """Анализирует все треки в папке и умно переименовывает её"""
        flac_files = [f for f in os.listdir(folder_path) if f.lower().endswith('.flac')]
        if not flac_files:
            return folder_path 
            
        print(f"\n📂 [RENAMER] Анализирую папку: {os.path.basename(folder_path)}")
            
        album_artists_list = []
        track_artists_sets = []
        album_names = []
        
        # 1. Собираем статистику
        for f in flac_files:
            audio = FLAC(os.path.join(folder_path, f))
            
            aa = audio.get('albumartist', [])
            if aa:
                album_artists_list.append(tuple(aa))
                
            a = audio.get('artist', [])
            if a:
                track_artists_sets.append(set(a))
                
            al = audio.get('album', audio.get('title', ['Unknown_Release']))
            album_names.append(al[0])

        # 2. Выбираем альбом
        best_album = Counter(album_names).most_common(1)[0][0] if album_names else "Unknown_Release"

        # 3. Вычисляем владельцев альбома
        final_artists = []
        if album_artists_list:
            best_aa = Counter(album_artists_list).most_common(1)[0][0]
            final_artists = list(best_aa)
        elif track_artists_sets:
            common_artists = set.intersection(*track_artists_sets)
            if common_artists:
                first_track_artists = FLAC(os.path.join(folder_path, flac_files[0])).get('artist', [])
                final_artists = [art for art in first_track_artists if art in common_artists]
            else:
                final_artists = ["Various Artists"]
        else:
            final_artists = ["Unknown Artist"]

        # 4. Формируем финальное имя
        artists_str = ", ".join(final_artists)
        new_foldername = self.clean_name(f"{artists_str} - {best_album}")
        
        parent_dir = os.path.dirname(folder_path)
        new_folder_path = os.path.join(parent_dir, new_foldername)
        
        print(f"   -> Ожидаемое имя: {new_foldername}")
        
        # 5. Проверки и переименование
        if folder_path == new_folder_path:
            print("   -> ✅ Имя папки уже идеальное, пропускаю.")
            return folder_path
            
        # Защита от дурака на Windows/NTFS (если поменялся только регистр)
        if folder_path.lower() == new_folder_path.lower():
            print("   -> 🔄 Изменение только регистра букв. Выполняю безопасное переименование...")
            temp_path = folder_path + "_temp_rename"
            os.rename(folder_path, temp_path)
            os.rename(temp_path, new_folder_path)
            print(f"   -> 📁 Папка переименована: {new_foldername}")
            return new_folder_path
            
        # Проверка, не мешает ли нам другая папка
        if os.path.exists(new_folder_path):
            print(f"   -> ⚠️ Ошибка: Папка с именем '{new_foldername}' уже существует рядом! Пропускаю.")
            return folder_path
            
        # Финальное переименование
        os.rename(folder_path, new_folder_path)
        print(f"   -> 📁 Папка переименована: {new_foldername}")
        return new_folder_path