import os
import requests
from mutagen.flac import FLAC

class LyricManager:
    def __init__(self):
        # LRCLIB просит указывать User-Agent
        self.headers = {
            'User-Agent': 'NaviSan_Local_Script (https://github.com/user/navisan)'
        }

    def _fetch_from_api(self, artist, title, album, duration):
        """Двухступенчатый и умный поиск текста в LRCLIB."""
        
        # Шаг 1: Строгий поиск
        exact_params = {
            'artist_name': artist,
            'track_name': title,
            'album_name': album,
            'duration': duration
        }
        try:
            res = requests.get("https://lrclib.net/api/get", params=exact_params, headers=self.headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                # Убеждаемся, что текст реально есть
                if data.get('syncedLyrics') or data.get('plainLyrics'):
                    return data
        except Exception as e:
            pass

        # Шаг 2: Мягкий поиск (если строгий не прошел из-за разницы в секундах или альбоме)
        search_query = f"{artist} {title}"
        try:
            res = requests.get("https://lrclib.net/api/search", params={'q': search_query}, headers=self.headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                
                if isinstance(data, list):
                    best_plain_match = None
                    
                    # Перебираем ВСЕ результаты поиска, а не только первый!
                    for item in data:
                        api_artist = item.get('artistName', '').lower()
                        api_title = item.get('trackName', '').lower()
                        
                        # Базовая защита: если ни артист, ни название не похожи — это левый трек, пропускаем
                        if artist.lower() not in api_artist and title.lower() not in api_title:
                            continue
                            
                        # Если нашли синхронизированный текст — это джекпот, берем его немедленно
                        if item.get('syncedLyrics'):
                            return item
                            
                        # Если нашли обычный текст, запоминаем его, но продолжаем искать синхронизированный
                        if item.get('plainLyrics') and not best_plain_match:
                            best_plain_match = item
                            
                    # Если синхронного так и не нашлось ни в одном результате, возвращаем хотя бы обычный
                    return best_plain_match
        except Exception as e:
            pass
            
        return None

    def process_file(self, filepath):
        """Скачивает текст и сохраняет его рядом с аудиофайлом."""
        if not filepath.lower().endswith('.flac'):
            return "Не аудиофайл"
            
        lrc_path = os.path.splitext(filepath)[0] + '.lrc'
        
        if os.path.exists(lrc_path):
            return "Уже есть 📁"
            
        try:
            audio = FLAC(filepath)
            
            artist = audio.get('artist', audio.get('albumartist', ['']))[0]
            title = audio.get('title', [''])[0]
            album = audio.get('album', [''])[0]
            
            if hasattr(audio.info, 'length'):
                duration = int(audio.info.length)
            else:
                duration = 0
            
            if not artist or not title:
                return "Нет тегов ❌"
                
            print(f"🎤 Ищу текст для: {artist} - {title}...")
            
            data = self._fetch_from_api(artist, title, album, duration)
            
            if data:
                lyrics = data.get('syncedLyrics')
                status = "Караоке (Синхронный) ✨"
                
                if not lyrics:
                    lyrics = data.get('plainLyrics')
                    status = "Обычный текст 📄"
                    
                if lyrics:
                    with open(lrc_path, 'w', encoding='utf-8') as f:
                        f.write(lyrics)
                    return status
            
            return "Не найдено ❌"
            
        except Exception as e:
            return f"Ошибка: {e}"