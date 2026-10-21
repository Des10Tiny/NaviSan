"""Модуль нейросетевого выравнивания текста песни по аудиофайлу (Forced Alignment).

Использует легковесный C++ рантайм faster-whisper (CTranslate2) без тяжелого PyTorch.
Модель загружается исключительно в момент выравнивания и сразу выгружается из памяти,
сохраняя 0 MB RAM в режиме ожидания.
"""

from __future__ import annotations

import gc
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Паттерн для заголовков секций Genius вида [Куплет 1], [Chorus], [Интро], [Outro] и т.д.
SECTION_HEADER_RE = re.compile(r"^\[[^\]]+\]$")


def _patch_av_open() -> None:
    """Безопасная обёртка над av.open для совместимости с новыми версиями PyAV."""
    try:
        import av
        if hasattr(av, "open"):
            orig_open = av.open

            def safe_open(*args, **kwargs):
                kwargs.pop("metadata_errors", None)
                return orig_open(*args, **kwargs)

            av.open = safe_open
    except Exception as e:
        log.debug("Не удалось пропатчить av.open: %s", e)


def _clean_word(word: str) -> str:
    """Очищает слово для fuzzy-сопоставления (нижний регистр, только буквы/цифры)."""
    return re.sub(r"[^\w]", "", (word or "").lower())


def align_lyrics(
    audio_path: str | Path,
    plain_text: str,
    language: str | None = None,
    model_size: str = "tiny",
    model: any = None,
) -> str:
    """Выравнивает текст песни по аудиофайлу, расставляя таймкоды [mm:ss.xx].

    Args:
        audio_path: Путь к аудиофайлу (FLAC/MP3/WAV).
        plain_text: Обычный текст песни (например, с Genius).
        language: Код языка ("ru", "en" и т.д.) или None для автоопределения.
        model_size: Размер модели faster-whisper ("tiny" по умолчанию).
        model: Опциональный уже загруженный экземпляр WhisperModel для пакетных операций.

    Returns:
        Синхронизированный текст в формате .lrc.
    """
    _patch_av_open()

    raw_lines = [l.strip() for l in plain_text.strip().split("\n")]
    if not raw_lines:
        return ""

    # Отфильтровываем пустые строки и служебные теги [Куплет], сохраняя структуру
    cleaned_lines: list[tuple[int, str, bool]] = []
    for idx, line in enumerate(raw_lines):
        if not line:
            continue
        is_header = bool(SECTION_HEADER_RE.match(line))
        cleaned_lines.append((idx, line, is_header))

    if not cleaned_lines:
        return plain_text

    # 1. Запуск транскрипции с таймкодами слов через faster-whisper
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        log.error("faster-whisper не установлен: %s", e)
        return plain_text

    whisper_words: list[dict] = []
    should_cleanup_model = False
    try:
        if model is None:
            log.info("Загрузка модели faster-whisper (%s)...", model_size)
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            should_cleanup_model = True

        segments, info = model.transcribe(
            str(audio_path),
            word_timestamps=True,
            language=language,
        )

        for segment in segments:
            if segment.words:
                for w in segment.words:
                    clean_w = _clean_word(w.word)
                    if clean_w:
                        whisper_words.append({
                            "word": clean_w,
                            "raw": w.word,
                            "start": w.start,
                            "end": w.end,
                        })
    except Exception as e:
        log.error("Ошибка транскрипции faster-whisper для %s: %s", audio_path, e)
        return plain_text
    finally:
        if should_cleanup_model and model is not None:
            del model
            gc.collect()

    if not whisper_words:
        log.warning("faster-whisper не обнаружил слов в аудио %s", audio_path)
        return plain_text

    # 2. Нечёткое выравнивание строк текста по распознанным таймкодам слов
    try:
        from rapidfuzz import fuzz
    except ImportError:
        fuzz = None

    synced_lines: list[str] = []
    w_idx = 0
    total_words = len(whisper_words)

    for orig_idx, line, is_header in cleaned_lines:
        # Служебные заголовки секций Genius ([Куплет 1], [Припев]) не выравниваем
        if is_header:
            synced_lines.append(line)
            continue

        line_words = [_clean_word(w) for w in line.split() if _clean_word(w)]
        if not line_words:
            synced_lines.append(line)
            continue

        start_time: float | None = None

        if w_idx < total_words:
            # Ищем начало строки по первым 2-3 словам с опережающим сканированием
            target = " ".join(line_words[:3])
            best_score = -1
            best_idx = w_idx
            scan_window = min(total_words, w_idx + 18)

            for scan_idx in range(w_idx, scan_window):
                cand_len = min(3, total_words - scan_idx)
                cand = " ".join(whisper_words[scan_idx + k]["word"] for k in range(cand_len))

                score = fuzz.ratio(target, cand) if fuzz else (100 if target in cand else 0)
                if score > best_score:
                    best_score = score
                    best_idx = scan_idx
                if score >= 90:
                    break

            start_time = whisper_words[best_idx]["start"]
            w_idx = min(total_words - 1, best_idx + max(1, len(line_words) - 1))

        if start_time is not None:
            total_sec = max(0.0, start_time)
            m = int(total_sec // 60)
            s = total_sec % 60
            synced_lines.append(f"[{m:02d}:{s:05.2f}] {line}")
        else:
            synced_lines.append(line)

    return "\n".join(synced_lines)
