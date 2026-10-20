#!/usr/bin/env python3
"""CLI утилита для пакетной «идеализации» всей музыкальной библиотеки NaviSan.

Позволяет одной командой привести всю существующую коллекцию к эталонному стандарту:
  1. Очистка от ID3-мусора и санитизация тегов (FLACSurgeon + TagSanitizer).
  2. Проверка и поиск недостающих качественных обложек (covers.musichoarders).
  3. Поиск и сохранение синхронизированных текстов (.lrc) с LRCLIB и Genius.
  4. Приведение треков к формату '01. Title.flac' и папок к 'Artist - Album'.

Использование:
  python batch.py              # Идеализация папки Library (по умолчанию)
  python batch.py --inbox      # Пакетная обработка всех релизов в Inbox
  python batch.py --force-cover# Принудительно перезаписать обложки
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from pipeline import upgrade_album, album_has_cover
from covers import search_covers


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Пакетная идеализация музыкальной библиотеки NaviSan"
    )
    parser.add_argument(
        "--target",
        choices=["library", "inbox"],
        default="library",
        help="Какую папку обрабатывать (по умолчанию: library)",
    )
    parser.add_argument(
        "--path",
        type=str,
        default=None,
        help="Пользовательский путь к каталогу с релизами",
    )
    parser.add_argument(
        "--force-cover",
        action="store_true",
        help="Искать и перезаписывать обложку, даже если в папке уже есть cover.jpg",
    )
    args = parser.parse_args()

    if args.path:
        target_dir = Path(args.path)
    elif args.target == "inbox":
        target_dir = Path(os.environ.get("NAVISAN_INBOX", "inbox"))
    else:
        target_dir = Path(os.environ.get("NAVISAN_LIBRARY", "library"))

    if not target_dir.exists():
        print(f"❌ Целевой каталог не существует: {target_dir}")
        sys.exit(1)

    # Ищем все папки с FLAC
    albums = sorted([
        d for d in target_dir.iterdir()
        if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.rglob("*.flac") if f.is_file())
    ])

    if not albums:
        print(f"📭 В каталоге {target_dir} не найдено папок с FLAC-файлами.")
        sys.exit(0)

    total = len(albums)
    print("=" * 65)
    print(f"🚀 NaviSan Batch: Идеализация библиотеки")
    print(f"📁 Каталог: {target_dir}")
    print(f"💿 Всего альбомов для обработки: {total}")
    print("=" * 65)

    processed = 0
    total_covers = 0
    total_lyrics = 0
    start_time = time.time()

    for i, album in enumerate(albums, 1):
        print(f"\n[{i}/{total}] 🎵 Обработка: {album.name}...")
        try:
            res = upgrade_album(album, force_cover=args.force_cover)
            if "error" in res:
                print(f"   ⚠️ Пропущен: {res['error']}")
                continue

            processed += 1
            cover_str = "🖼️ +обложка" if res.get("cover_added") else "🖼️ обложка OK"
            if res.get("cover_added"):
                total_covers += 1

            n_lyrics = res.get("lyrics_added", 0)
            total_lyrics += n_lyrics
            lyrics_str = f"📝 +{n_lyrics} .lrc" if n_lyrics > 0 else "📝 тексты OK"

            renamed_str = ""
            if res.get("old_folder") != res.get("new_folder"):
                renamed_str = f" -> 📂 {res['new_folder']}"

            print(f"   ✅ Готово ({res.get('tracks_count', 0)} треков) | {cover_str} | {lyrics_str}{renamed_str}")
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")

    elapsed = time.time() - start_time
    print("\n" + "=" * 65)
    print(f"🎉 Идеализация завершена за {elapsed:.1f} сек!")
    print(f"💿 Успешно обработано: {processed}/{total} альбомов")
    print(f"🖼️ Добавлено обложек:  {total_covers}")
    print(f"📝 Загружено текстов:  {total_lyrics}")
    print("=" * 65)


if __name__ == "__main__":
    main()
