import os
import requests
import urllib.parse
import asyncio
import websockets
import json
import webbrowser
from mutagen.flac import FLAC, Picture

from sanitizer import TagSanitizer

class FLACSurgeon:
    def __init__(self, sanitizer):
        self.sanitizer = sanitizer
        
        self.whitelist = {
            'title', 'artist', 'albumartist', 'album', 
            'date', 'year', 'originaldate', 'tracknumber', 'tracktotal', 
            'discnumber', 'disctotal', 'genre', 'label', 'organization',
            'rating', 'itunesadvisory',
            'replaygain_track_gain', 'replaygain_track_peak', 
            'replaygain_album_gain', 'replaygain_album_peak'
        }

    def _interactive_fetch_cover(self, artist, album, title, folder_path):
        """Интерактивный поиск. Теперь возвращает кортеж: (Картинка, Дата)"""
        primary_artist = self.sanitizer.split_pattern.split(artist)[0].strip()
        is_album = bool(album and album.lower() != title.lower())
        search_release = album if is_album else title
        
        # Добавляем поле date в словарь для извлечения
        fetch_result = {"url": None, "date": None}

        async def run_ws_server():
            stop_event = asyncio.Event()

            async def handler(websocket):
                try:
                    await websocket.send(json.dumps({
                        "type": "text", 
                        "text": f"NaviSan: Выбери обложку для {primary_artist} - {search_release}"
                    }))
                    
                    async for message in websocket:
                        data = json.loads(message)
                        if data.get("type") == "pick":
                            fetch_result["url"] = data.get("bigCoverUrl")
                            # Вытаскиваем дату прямо из ответа сайта!
                            fetch_result["date"] = data.get("releaseInfo", {}).get("date")
                            
                            print(f"\n🎯 [WEBSOCKET] Сайт передал ссылку: {fetch_result['url']}")
                            if fetch_result["date"]:
                                print(f"📅 [WEBSOCKET] Сайт подсказал год релиза: {fetch_result['date']}")
                                
                            await websocket.send(json.dumps({"type": "close"}))
                            stop_event.set()
                            break
                except websockets.exceptions.ConnectionClosed:
                    print("⚠️ [WEBSOCKET] Вкладка закрыта до выбора картинки.")
                    stop_event.set() 

            params = urllib.parse.urlencode({
                'artist': primary_artist,
                'album': search_release,
                'sources': 'amazonmusic,applemusic,deezer,spotify,tidal',
                'remote.port': 'ws:8765',
                'remote.agent': 'NaviSan_Local'
            })
            url = f"https://covers.musichoarders.xyz/?{params}"
            
            print(f"\n🌐 [БРАУЗЕР] Открываю: {url}")
            webbrowser.open(url)
            
            print("🎧 [СЕРВЕР] Жду клика на сайте...")
            async with websockets.serve(handler, "localhost", 8765):
                await stop_event.wait()

        try:
            asyncio.run(run_ws_server())
        except Exception as e:
            print(f"❌ [ОШИБКА СЕРВЕРА] {e}")

        target_url = fetch_result["url"]
        target_date = fetch_result["date"]
        
        if target_url:
            print(f"⏳ [СЕТЬ] Начинаю скачивание картинки...")
            try:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8'
                }
                
                domain = urllib.parse.urlparse(target_url).netloc
                print(f"📡 [СЕТЬ] Отправляю GET-запрос к {domain}...")
                
                response = requests.get(target_url, headers=headers, timeout=15)
                response.raise_for_status() 
                
                img_data = response.content
                print(f"📦 [ДАННЫЕ] Скачано {len(img_data)} байт")
                
                if len(img_data) == 0:
                    return None, target_date
                
                cover_path = os.path.join(folder_path, 'cover.jpg')
                with open(cover_path, 'wb') as f:
                    f.write(img_data)
                    
                print("✅ [УСПЕХ] Обложка сохранена на диск!")
                return img_data, target_date
                
            except Exception as e:
                print(f"❌ [ОШИБКА СКАЧИВАНИЯ] {e}")
                
        return None, target_date

    def _embed_cover(self, audio, folder_path, artist, album, title, force_update=False):
        """Вшивает обложку и возвращает дату релиза (если она была получена)."""
        if force_update and audio.pictures:
            audio.clear_pictures()

        fetched_date = None
        cover_path = os.path.join(folder_path, 'cover.jpg')
        img_data = None

        if os.path.exists(cover_path) and not force_update:
            # Если картинка уже есть локально - браузер не открываем
            if not audio.pictures:
                with open(cover_path, 'rb') as f:
                    img_data = f.read()
                print("📁 Использован локальный файл cover.jpg")
        elif artist and (album or title):
            # Если картинки нет, идем в браузер
            img_data, fetched_date = self._interactive_fetch_cover(artist, album, title, folder_path)

        if img_data and not audio.pictures:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Front Cover"
            pic.data = img_data
            audio.add_picture(pic)
            
        return fetched_date

    def heal_file(self, filepath, force_update=False):
        """Главный конвейер очистки файла."""
        if not filepath.lower().endswith('.flac'):
            return
            
        try:
            audio = FLAC(filepath)
        except Exception as e:
            print(f"❌ Ошибка чтения (файл поврежден): {os.path.basename(filepath)}")
            return

        # 1. ЧИСТКА ОТ МУСОРА И БИТЫХ ЗНАЧЕНИЙ
        saved_tags = {}
        for key in list(audio.keys()):
            lower_key = key.lower()
            if lower_key in self.whitelist:
                # Фильтруем каждое значение внутри тега
                cleaned_vals = []
                for v in audio[key]:
                    v_str = str(v).strip()
                    
                    if not v_str: 
                        continue # Игнорируем пустые строки
                    if lower_key in ['year', 'date', 'originaldate'] and v_str in ['0', '0000']:
                        continue # Уничтожаем битые нули в датах
                    if lower_key in ['tracktotal', 'disctotal'] and v_str == '0':
                        continue # Уничтожаем нули в количестве дисков/треков
                    if lower_key == 'discnumber' and v_str == '0':
                        v_str = '1' # Нулевых дисков не бывает, исправляем на 1
                    if lower_key == 'label' and v_str.lower() in ['[no label]', 'no label']:
                        continue
                        
                    cleaned_vals.append(v_str)
                
                # Если после чистки тег не опустел, сохраняем его
                if cleaned_vals:
                    saved_tags[lower_key] = cleaned_vals
                    
            del audio[key] # Удаляем старый грязный тег

        # 2. ВОССТАНОВЛЕНИЕ ИДЕАЛЬНЫХ ТЕГОВ
        for key, val in saved_tags.items():
            audio[key] = val

        # 3. ОБРАБОТКА ИМЕН АРТИСТОВ
        if 'artist' in audio:
            raw_artist_string = " & ".join(audio['artist']) 
            audio['artist'] = self.sanitizer.process_artists(raw_artist_string)

        if 'albumartist' in audio:
            raw_albumartist_string = " & ".join(audio['albumartist'])
            audio['albumartist'] = self.sanitizer.process_artists(raw_albumartist_string)

        # 4. ОБЛОЖКА И ДАТА ИЗ ИНТЕРНЕТА
        artist_name = audio.get('albumartist', audio.get('artist', ['']))[0]
        album_name = audio.get('album', [''])[0]
        title_name = audio.get('title', [''])[0]
        folder_path = os.path.dirname(filepath)
        
        # Получаем дату с сайта обложек (если мы его открывали)
        fetched_date = self._embed_cover(audio, folder_path, artist_name, album_name, title_name, force_update)
        
        # Если с сайта пришла дата, а в нашем вычищенном файле её нет — вписываем!
        if fetched_date and 'date' not in audio and 'year' not in audio:
            audio['date'] = [str(fetched_date)]
            print(f"🔧 [АВТО-ИСПРАВЛЕНИЕ] Добавлен год релиза: {fetched_date}")

        # 5. ФИНАЛЬНАЯ СИНХРОНИЗАЦИЯ ДАТ
        # Плееры бывают капризными. Делаем так, чтобы DATE и YEAR всегда были заполнены одинаково
        if 'date' in audio and 'year' not in audio:
            audio['year'] = [str(audio['date'][0])[:4]] # Берем первые 4 цифры (год)
        elif 'year' in audio and 'date' not in audio:
            audio['date'] = audio['year']

        # 6. СОХРАНЕНИЕ
        audio.save()
        print(f"✨ Готово: {os.path.basename(filepath)}")