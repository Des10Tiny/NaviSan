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

        # Автоопределение языка по тексту песни, если не задан явно
        if not language and plain_text:
            if re.search(r"[а-яА-ЯёЁ]", plain_text):
                language = "ru"
            elif re.search(r"[a-zA-Z]", plain_text):
                language = "en"

        # Первые слова песни в качестве initial_prompt (до 50 слов без заголовков в скобках)
        words_prompt = " ".join([w for w in plain_text.split() if not (w.startswith("[") and w.endswith("]"))][:50])

        segments, info = model.transcribe(
            str(audio_path),
            word_timestamps=True,
            language=language,
            condition_on_previous_text=False,
            vad_filter=True,
            initial_prompt=words_prompt or None,
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

    line_results: list[dict] = []
    w_idx = 0
    total_words = len(whisper_words)
    unmatched_streak = 0
    matched_lines_count = 0
    total_vocal_lines = 0

    for orig_idx, line, is_header in cleaned_lines:
        # Служебные заголовки секций Genius ([Куплет 1], [Припев]) не выравниваем
        if is_header:
            line_results.append({"line": line, "time": None, "is_header": True})
            continue

        line_words = [_clean_word(w) for w in line.split() if _clean_word(w)]
        if not line_words:
            line_results.append({"line": line, "time": None, "is_header": False})
            continue

        total_vocal_lines += 1
        start_time: float | None = None

        if w_idx < total_words:
            # Ищем начало строки по первым 3-4 словам с адаптивным опережающим окном
            words_to_match = min(4, len(line_words))
            target = " ".join(line_words[:words_to_match])
            best_score = -1
            best_idx = w_idx
            # Окно поиска расширяется при нескольких подряд ненайденных строках
            scan_window = min(total_words, w_idx + 25 + unmatched_streak * 10)

            for scan_idx in range(w_idx, scan_window):
                cand_len = min(words_to_match, total_words - scan_idx)
                cand = " ".join(whisper_words[scan_idx + k]["word"] for k in range(cand_len))

                if fuzz:
                    # Используем максимум между ratio и token_sort_ratio для компенсации пропущенных предлогов (и, а, в)
                    score = max(fuzz.ratio(target, cand), fuzz.token_sort_ratio(target, cand))
                else:
                    score = 100 if target in cand else 0

                if score > best_score:
                    best_score = score
                    best_idx = scan_idx
                if score >= 90:
                    break

            # Принимаем таймкод только если сходство слов достаточного качества (>= 55%)
            if best_score >= 55:
                start_time = whisper_words[best_idx]["start"]
                # Продвигаем указатель ровно на фактически распознанные Whisper слова этой строки,
                # а не на длину всей строки текста, чтобы не обогнать аудио
                matched_span = min(words_to_match, len(line_words))
                for k in range(matched_span, min(len(line_words), total_words - best_idx)):
                    w_cand = whisper_words[best_idx + k]["word"]
                    w_ref = line_words[k]
                    sim = fuzz.ratio(w_ref, w_cand) if fuzz else (100 if w_ref == w_cand else 0)
                    if sim >= 60:
                        matched_span = k + 1
                    else:
                        break

                w_idx = min(total_words - 1, best_idx + max(1, matched_span))
                unmatched_streak = 0
                matched_lines_count += 1
            else:
                unmatched_streak += 1

        line_results.append({"line": line, "time": start_time, "is_header": False})

    # Проверка на полное несовпадение текста и аудио (чужая песня)
    if total_vocal_lines > 0:
        match_ratio = matched_lines_count / total_vocal_lines
        if match_ratio < 0.35:
            log.warning(
                "Forced alignment mismatch: текст не соответствует аудио %s (совпало всего %d/%d строк, %.1f%%). Выравнивание отменено.",
                audio_path,
                matched_lines_count,
                total_vocal_lines,
                match_ratio * 100,
            )
            return plain_text

    # Интерполяция таймкодов для пропущенных вокальных строк (если песня совпала)
    vocal_indices = [i for i, r in enumerate(line_results) if not r["is_header"] and r["line"].strip()]
    for i, idx in enumerate(vocal_indices):
        if line_results[idx]["time"] is None:
            prev_t = None
            for p in range(i - 1, -1, -1):
                t = line_results[vocal_indices[p]]["time"]
                if t is not None:
                    prev_t = t
                    break
            next_t = None
            for n in range(i + 1, len(vocal_indices)):
                t = line_results[vocal_indices[n]]["time"]
                if t is not None:
                    next_t = t
                    break

            if prev_t is not None and next_t is not None:
                line_results[idx]["time"] = prev_t + (next_t - prev_t) * 0.5
            elif prev_t is not None:
                line_results[idx]["time"] = prev_t + 2.5
            elif next_t is not None:
                line_results[idx]["time"] = max(0.0, next_t - 2.5)

    synced_lines: list[str] = []
    for r in line_results:
        t = r["time"]
        if t is not None:
            total_sec = max(0.0, t)
            m = int(total_sec // 60)
            s = total_sec % 60
            synced_lines.append(f"[{m:02d}:{s:05.2f}] {r['line']}")
        else:
            synced_lines.append(r["line"])

    return "\n".join(synced_lines)
