"""Модуль нейросетевого выравнивания текста песни по аудиофайлу (Forced Alignment).

Использует легковесный C++ рантайм faster-whisper (CTranslate2) без тяжелого PyTorch
и алгоритм глобального динамического программирования Needleman-Wunsch (портированный из syncalong)
для строго монотонного и точного сопоставления слов песни.

Модель загружается исключительно в момент выравнивания и сразу выгружается из памяти,
сохраняя 0 MB RAM в режиме ожидания.
"""

from __future__ import annotations

import gc
import logging
import re
from functools import cache
from pathlib import Path

log = logging.getLogger(__name__)

# Паттерн для заголовков секций Genius вида [Куплет 1], [Chorus], [Интро], [Outro] и т.д.
SECTION_HEADER_RE = re.compile(r"^\[[^\]]+\]$")

# Паттерн для технических метаданных азиатских платформ (NetEase/KuGou) и тегов LRC
METADATA_LINE_RE = re.compile(
    r"^(?:"
    r"\[(?:ar|al|ti|au|by|offset|length|re|ve):.*\]|"
    r"(?:作词|作曲|编曲|制作人|录音|混音|母带|吉他|贝斯|鼓|键盘|和声|弦乐|策划|监制|企划|发行)\s*[:：].*|"
    r"(?:lyrics\s*by|written\s*by|composed\s*by|produced\s*by|arranged\s*by)\s*[:：].*|"
    r"(?:qq音乐|网易云音乐|酷狗音乐).*"
    r")$",
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


def _clean_word(word: str) -> str:
    """Очищает слово для fuzzy-сопоставления (нижний регистр, только буквы/цифры)."""
    return re.sub(r"[^\w]", "", (word or "").lower())


try:
    from rapidfuzz.fuzz import ratio as _rf_ratio

    def _ratio(a: str, b: str) -> float:
        return float(_rf_ratio(a, b))

except ImportError:
    from difflib import SequenceMatcher

    def _ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio() * 100.0


@cache
def _word_score(a: str, b: str) -> float:
    """Возвращает схожесть 0–100 между двумя нормализованными словами с кэшированием."""
    if a == b:
        return 100.0
    return _ratio(a, b)


def _dp_align(
    lyric_words: list[str],
    transcript_words: list[dict],
    threshold: float = 55.0,
) -> dict[int, int]:
    """Глобальное динамическое программирование Needleman-Wunsch (алгоритм syncalong).

    Строит строго монотонное соответствие {lyric_word_idx: transcript_word_idx},
    максимизирующее суммарный score совпадений слов.

    Args:
        lyric_words: Список всех нормализованных слов текста по порядку.
        transcript_words: Список распознанных Whisper слов [{'word', 'start', 'end'}].
        threshold: Минимальный fuzzy score (0-100) для зачёта совпадения.

    Returns:
        Словарь {индекс_слова_текста: индекс_слова_в_аудио}.
    """
    n = len(lyric_words)
    m = len(transcript_words)

    if n == 0 or m == 0:
        return {}

    SKIP_LYRIC_PENALTY = -1.0  # Небольшой штраф за пропуск слова из текста

    size = (n + 1) * (m + 1)
    dp = [0.0] * size
    trace = [0] * size  # 1: skip transcript, 2: skip lyric, 3: match

    # Инициализация первой колонки (пропуск слов текста)
    for i in range(1, n + 1):
        dp[i * (m + 1)] = dp[(i - 1) * (m + 1)] + SKIP_LYRIC_PENALTY
        trace[i * (m + 1)] = 2

    # Заполнение матрицы DP
    for i in range(1, n + 1):
        lw = lyric_words[i - 1]
        row_offset = i * (m + 1)
        prev_row_offset = (i - 1) * (m + 1)

        for j in range(1, m + 1):
            tw = transcript_words[j - 1]["word"]

            # Вариант 1: пропуск слова аудио
            best = dp[row_offset + j - 1]
            best_t = 1

            # Вариант 2: пропуск слова текста
            val = dp[prev_row_offset + j] + SKIP_LYRIC_PENALTY
            if val > best:
                best = val
                best_t = 2

            # Вариант 3: сопоставление слов
            score = _word_score(lw, tw)
            if score >= threshold:
                val = dp[prev_row_offset + j - 1] + score
                if val > best:
                    best = val
                    best_t = 3

            dp[row_offset + j] = best
            trace[row_offset + j] = best_t

    # Обратный ход (Traceback)
    mapping: dict[int, int] = {}
    i, j = n, m
    while i > 0 and j > 0:
        t = trace[i * (m + 1) + j]
        if t == 3:
            mapping[i - 1] = j - 1
            i -= 1
            j -= 1
        elif t == 2:
            i -= 1
        else:
            j -= 1

    return mapping


def _interpolate_gaps(
    lines: list[tuple[int, str, bool]],
    timestamps: dict[int, float],
    matched: list[int],
) -> None:
    """Интерполирует таймкоды для нераспознанных строк между подтверждёнными анкорами."""
    for k in range(len(matched) - 1):
        start_li = matched[k]
        end_li = matched[k + 1]
        gap = end_li - start_li

        if gap <= 1:
            continue

        t_start = timestamps[start_li]
        t_end = timestamps[end_li]

        for offset in range(1, gap):
            li = start_li + offset
            _, text, is_header = lines[li]
            if is_header or not text.strip():
                continue
            frac = offset / gap
            timestamps[li] = t_start + frac * (t_end - t_start)


def _extrapolate_edges(
    lines: list[tuple[int, str, bool]],
    timestamps: dict[int, float],
    matched: list[int],
    *,
    t_min: float,
    t_max: float,
) -> None:
    """Экстраполирует таймкоды для строк до первого и после последнего распознавания."""
    first, last = matched[0], matched[-1]

    gap_first = first - (-1)
    for li in range(first):
        _, text, is_header = lines[li]
        if is_header or not text.strip():
            continue
        frac = (li + 1) / gap_first
        timestamps[li] = max(0.0, t_min + frac * (timestamps[first] - t_min))

    gap_last = len(lines) - last
    for li in range(last + 1, len(lines)):
        _, text, is_header = lines[li]
        if is_header or not text.strip():
            continue
        frac = (li - last) / gap_last
        timestamps[li] = timestamps[last] + frac * (t_max - timestamps[last])


def align_lyrics(
    audio_path: str | Path,
    plain_text: str,
    language: str | None = None,
    model_size: str = "tiny",
    model: any = None,
) -> str:
    """Выравнивает текст песни по аудиофайлу с помощью глобального DP Needleman-Wunsch."""
    _patch_av_open()

    raw_lines = [l.strip() for l in plain_text.strip().split("\n")]
    if not raw_lines:
        return ""

    # Отфильтровываем метаданные, пустые строки и помечаем служебные теги [Куплет]
    cleaned_lines: list[tuple[int, str, bool]] = []
    for idx, line in enumerate(raw_lines):
        if not line:
            continue
        cleaned_content = re.sub(r"^\[\d{2}:\d{2}(?:\.\d+)?\]\s*", "", line).strip()
        if not cleaned_content or METADATA_LINE_RE.match(cleaned_content):
            continue
        is_header = bool(SECTION_HEADER_RE.match(line))
        cleaned_lines.append((idx, line, is_header))

    if not cleaned_lines:
        return plain_text

    # 1. Запуск транскрипции через faster-whisper
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
            vad_filter=False,  # ВАЖНО: False для музыки, чтобы не вырезать вокал под битом
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

    # 2. Подготовка плоского списка слов текста с обратной привязкой к строкам
    flat_words: list[str] = []
    word_to_line: list[int] = []
    total_vocal_lines = 0

    for li, (_, line, is_header) in enumerate(cleaned_lines):
        if is_header:
            continue
        line_w = [_clean_word(w) for w in line.split() if _clean_word(w)]
        if line_w:
            total_vocal_lines += 1
            for w in line_w:
                flat_words.append(w)
                word_to_line.append(li)

    if not flat_words:
        return plain_text

    # 3. Выравнивание через Needleman-Wunsch Dynamic Programming (syncalong)
    mapping = _dp_align(flat_words, whisper_words, threshold=55.0)

    # Привязка первого распознанного слова каждой строки к её таймкоду
    line_timestamps: dict[int, float] = {}
    for flat_idx, trans_idx in sorted(mapping.items()):
        li = word_to_line[flat_idx]
        ts = whisper_words[trans_idx]["start"]
        if li not in line_timestamps:
            line_timestamps[li] = ts

    # 4. Проверка на полное несовпадение песни (чужой текст)
    matched_lines_count = len(line_timestamps)
    if total_vocal_lines > 0:
        match_ratio = matched_lines_count / total_vocal_lines
        if match_ratio < 0.25:
            log.warning(
                "Forced alignment mismatch: текст не соответствует аудио %s (совпало всего %d/%d строк, %.1f%%). Выравнивание отменено.",
                audio_path,
                matched_lines_count,
                total_vocal_lines,
                match_ratio * 100,
            )
            return plain_text

    # 5. Интерполяция и экстраполяция пропущенных строк
    matched_indices = sorted(line_timestamps.keys())
    if matched_indices:
        _interpolate_gaps(cleaned_lines, line_timestamps, matched_indices)
        _extrapolate_edges(
            cleaned_lines,
            line_timestamps,
            matched_indices,
            t_min=whisper_words[0]["start"],
            t_max=whisper_words[-1]["end"],
        )

    # 6. Сборка итогового .lrc
    synced_lines: list[str] = []
    for li, (_, line, is_header) in enumerate(cleaned_lines):
        if is_header:
            synced_lines.append(line)
            continue
        t = line_timestamps.get(li)
        if t is not None:
            total_sec = max(0.0, t)
            m = int(total_sec // 60)
            s = total_sec % 60
            synced_lines.append(f"[{m:02d}:{s:05.2f}] {line}")
        else:
            synced_lines.append(line)

    return "\n".join(synced_lines)
