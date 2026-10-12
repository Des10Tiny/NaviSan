import asyncio
import websockets
import json
import webbrowser
import urllib.parse

# Событие для остановки сервера после получения картинки
stop_event = asyncio.Event()

async def handler(websocket):
    print("🔗 Сайт подключился к нашему скрипту!")
    
    # Меняем текст в верхней плашке сайта (как требует пункт 3 доков)
    await websocket.send(json.dumps({
        "type": "text", 
        "text": "NaviSan: Выбери лучшую обложку!"
    }))
    
    try:
        async for message in websocket:
            print("\n🎉 БИНГО! Сайт прислал данные при клике. Вот структура:")
            # Выводим первые 1000 символов, чтобы не засорить консоль, если там Base64
            print(message[:1000]) 
            
            # Посылаем команду на закрытие вкладки
            await websocket.send(json.dumps({"type": "close"}))
            
            # Даем сигнал на остановку сервера
            stop_event.set()
            break
    except websockets.exceptions.ConnectionClosed:
        print("Вкладка закрыта.")

async def main():
    # Тестовые данные (можешь поменять на свои проблемные треки)
    artist = "TumaniYO"
    album = "CONTRAST"
    
    # Параметры запроса (remote.port укажет сайту стучаться к нам на localhost:8765)
    params = urllib.parse.urlencode({
        'artist': artist,
        'album': album,
        'remote.port': 'ws:8765',
        'remote.agent': 'NaviSan_Local'
    })
    url = f"https://covers.musichoarders.xyz/?{params}"
    
    print(f"🌐 Открываем браузер: {url}")
    webbrowser.open(url)
    
    print("🎧 Запускаем локальный WebSocket-сервер на порту 8765 и ждем твоего клика...")
    async with websockets.serve(handler, "localhost", 8765):
        await stop_event.wait() # Ждем, пока не сработает stop_event.set()

if __name__ == "__main__":
    asyncio.run(main())