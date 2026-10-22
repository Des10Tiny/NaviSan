"""Модуль нейросетевого выравнивания текста песни по аудиофайлу (Forced Alignment).

Использует C++ рантайм faster-whisper (CTranslate2) для транскрипции
и lyric-align для устойчивого character-level анкоринга строк на таймлайне аудио.
"""

from __future__ import annotations

import gc
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Паттерн для заголовков секций Genius ([Куплет 1], [Chorus], [Интро] и т.д.)
SECTION_HEADER_RE = re.compile(r"^[\[(][^\[\]()]{1,60}[\])]$")

# Паттерн для китайских метаданных из KuGou/NetEase (作词, 作曲, 编曲 и т.д.)
CJK_META_RE = re.compile(
    r"^(?:\[\d{2}:\d{2}\.\d+\]\s*)?(?:作词|作曲|编曲|制作人|词|曲|录音|混音|母带|监制)\s*[:：]",
    re.I,
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

    # Отфильтровываем пустые строки, служебные комментарии, китайские кредиты и заголовки секций
    lyric_lines: list[str] = []
    for line in raw_lines:
        if not line or line.startswith("#"):
            continue
        if CJK_META_RE.match(line):
            continue
        if SECTION_HEADER_RE.match(line):
            continue
        lyric_lines.append(line)

    if not lyric_lines:
        return plain_text

    # 1. Запуск транскрипции через faster-whisper
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        log.error("faster-whisper не установлен: %s", e)
        return plain_text

    should_cleanup_model = False
    segments_raw = []
    try:
        if model is None:
            log.info("Загрузка модели faster-whisper (%s)...", model_size)
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            should_cleanup_model = True

        # Автоопределение языка по тексту песни, если не задан явно
        if not language and plain_text:
            if re.search(r"[а-яА-ЯёЁ]", plain_text):
                language = "ru"
            elif re.search(r"[a-zA-Z]", plain_text):
                language = "en"

        # Первые слова песни в качестве подсказки для словаря Whisper
        prompt_words = [w for w in plain_text.split() if not (w.startswith("[") and w.endswith("]"))]
        initial_prompt = " ".join(prompt_words[:50]) if prompt_words else None

        # ВАЖНО: vad_filter=False! Для музыки с плотным битом / басом / автотюном VAD
        # ошибочно считает музыку шумом и вырезает до 95% аудиопотока.
        segments_gen, info = model.transcribe(
            str(audio_path),
            word_timestamps=True,
            language=language,
            condition_on_previous_text=False,
            vad_filter=False,
            initial_prompt=initial_prompt,
        )
        segments_raw = list(segments_gen)
    except Exception as e:
        log.error("Ошибка транскрипции faster-whisper для %s: %s", audio_path, e)
        return plain_text
    finally:
        if should_cleanup_model and model is not None:
            del model
            gc.collect()

    if not segments_raw:
        log.warning("faster-whisper не обнаружил сегментов в %s", audio_path)
        return plain_text

    # 2. Выравнивание через специализированную библиотеку lyric-align
    try:
        from lyric_align import align, interpolate_gaps
        from lyric_align.model import Segment, Word
        from lyric_align.formats import to_lrc

        fw_segments = []
        for s in segments_raw:
            words = [Word(float(w.start), float(w.end), w.word) for w in (s.words or [])]
            fw_segments.append(Segment(float(s.start), float(s.end), s.text.strip(), words))

        # Сравниваем стратегию построчного сопоставления (pairing=1) и авто-группировки
        aligned_1 = align(fw_segments, lyric_lines, pairing=1)
        aligned_auto = align(fw_segments, lyric_lines, pairing="auto")

        m1 = sum(1 for a in aligned_1 if a.matched)
        ma = sum(1 for a in aligned_auto if a.matched)

        best_aligned = aligned_1 if m1 >= ma else aligned_auto
        best_matches = max(m1, ma)
        total_lines = len(lyric_lines)
        match_ratio = (best_matches / total_lines) if total_lines > 0 else 0.0

        # Защита от чужого текста песни (несоответствие аудио)
        if match_ratio < 0.25:
            log.warning(
                "Forced alignment mismatch: текст не соответствует аудио %s (совпало всего %d/%d строк, %.1f%%). Выравнивание отменено.",
                audio_path,
                best_matches,
                total_lines,
                match_ratio * 100,
            )
            return plain_text

        interpolated = interpolate_gaps(best_aligned)
        synced_lrc = to_lrc(interpolated).strip()
        return synced_lrc if synced_lrc else plain_text

    except Exception as e:
        log.error("Ошибка при выравнивании через lyric-align: %s", e)
        return plain_text
