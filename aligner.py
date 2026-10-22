"""Модуль нейросетевого выравнивания текста песни по аудиофайлу (Forced Alignment).

Объединяет:
- faster-whisper (CTranslate2): легковесный C++ рантайм без тяжелого PyTorch (0 MB RAM в простое).
- syncalong: алгоритм глобального динамического программирования Needleman-Wunsch (DP) для
  математически эталонного монотонного выравнивания слов и интерполяции таймкодов.
"""

from __future__ import annotations

import gc
import logging
import os
import re
from pathlib import Path

from syncalong.align import _dp_align, align_lyrics_to_transcript
from syncalong.formatter import format_lrc
from syncalong.lyrics import lyrics_prompt, parse_lyrics_text
from syncalong.textnorm import normalize
from syncalong.transcribe import WordTimestamp

log = logging.getLogger(__name__)

# Регулярное выражение для фильтрации метаданных NetEase / KuGou и стандартных тегов
METADATA_LINE_RE = re.compile(
    r"^(?:作词|作曲|编曲|制作人|录音|混音|母带|吉他|贝斯|鼓|键盘|和声|弦乐|监制|企划|统筹)\s*[:：]|^\[(?:ar|al|ti|by|offset|length):",
    re.IGNORECASE,
)


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


def clean_lyrics_metadata(text: str) -> str:
    """Очищает текст от технических кредитов и служебных мета-строк NetEase / KuGou."""
    if not text:
        return ""
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        content = re.sub(r"^\[\d{2}:\d{2}\.\d+\]\s*", "", stripped).strip()
        if METADATA_LINE_RE.match(content):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def align_lyrics(
    audio_path: str | Path,
    plain_text: str,
    language: str | None = None,
    model_size: str | None = None,
    model: any = None,
    threshold: float = 55.0,
) -> str:
    """Выравнивает текст песни по аудиофайлу через faster-whisper + syncalong Needleman-Wunsch DP.

    Args:
        audio_path: Путь к аудиофайлу (FLAC/MP3/WAV).
        plain_text: Обычный текст песни (например, с Genius).
        language: Код языка ("ru", "en" и т.д.) или None для автоопределения.
        model_size: Размер модели faster-whisper ("base" по умолчанию).
        model: Опциональный уже загруженный экземпляр WhisperModel для пакетных операций.
        threshold: Минимальный порог нечёткого сходства слов (по умолчанию 55.0).

    Returns:
        Синхронизированный текст в формате .lrc.
    """
    _patch_av_open()

    cleaned_text = clean_lyrics_metadata(plain_text)
    if not cleaned_text:
        return ""

    parsed_lines = parse_lyrics_text(cleaned_text)
    vocal_lines = [line for line in parsed_lines if not line.is_blank and line.words]
    if not vocal_lines:
        return cleaned_text

    # 1. Запуск транскрипции с таймкодами слов через faster-whisper
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        log.error("faster-whisper не установлен: %s", e)
        return cleaned_text

    if not model_size:
        model_size = os.getenv("NAVISAN_WHISPER_MODEL", "base")

    whisper_words: list[WordTimestamp] = []
    should_cleanup_model = False
    try:
        if model is None:
            log.info("Загрузка модели faster-whisper (%s)...", model_size)
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            should_cleanup_model = True

        # Автоопределение языка по тексту песни, если не задан явно
        if not language and cleaned_text:
            if re.search(r"[а-яА-ЯёЁ]", cleaned_text):
                language = "ru"
            elif re.search(r"[a-zA-Z]", cleaned_text):
                language = "en"

        # Первые строки песни в качестве подсказки (initial_prompt) для vocabulary biasing
        prompt = lyrics_prompt(parsed_lines)

        # vad_filter=False — критично для музыки, иначе VAD вырезает до 95% аудио на громком басе/бите
        segments, info = model.transcribe(
            str(audio_path),
            word_timestamps=True,
            language=language,
            condition_on_previous_text=False,
            vad_filter=False,
            initial_prompt=prompt or None,
        )

        for segment in segments:
            if segment.words:
                for w in segment.words:
                    norm_w = normalize(w.word)
                    if norm_w:
                        whisper_words.append(
                            WordTimestamp(
                                word=norm_w,
                                raw=w.word,
                                start=w.start,
                                end=w.end,
                            )
                        )
    except Exception as e:
        log.error("Ошибка транскрипции faster-whisper для %s: %s", audio_path, e)
        return cleaned_text
    finally:
        if should_cleanup_model and model is not None:
            del model
            gc.collect()

    if not whisper_words:
        log.warning("faster-whisper не обнаружил слов в аудио %s", audio_path)
        return cleaned_text

    # 2. Проверка на соответствие текста и аудио (защита от чужой песни)
    # Вычисляем матрицу DP и проверяем долю вокальных строк, где совпали слова
    flat_words: list[str] = []
    word_to_line: list[int] = []
    for li, line in enumerate(parsed_lines):
        for w in line.words:
            flat_words.append(w)
            word_to_line.append(li)

    mapping = _dp_align(flat_words, whisper_words, threshold=threshold)
    matched_lines = {word_to_line[i] for i in mapping.keys()}
    total_vocal = len(vocal_lines)

    if total_vocal > 0:
        match_ratio = len(matched_lines) / total_vocal
        if match_ratio < 0.25:
            log.warning(
                "Forced alignment mismatch: текст не соответствует аудио %s (совпало всего %d/%d строк, %.1f%%). Выравнивание отменено.",
                audio_path,
                len(matched_lines),
                total_vocal,
                match_ratio * 100,
            )
            return cleaned_text

    # 3. Глобальное динамическое программирование syncalong (Needleman-Wunsch DP) + интерполяция
    timed_lines = align_lyrics_to_transcript(parsed_lines, whisper_words, threshold=threshold)
    return format_lrc(timed_lines)
