from __future__ import annotations

import re
from dataclasses import dataclass

# Subtitle-credit artifacts Whisper's Russian model hallucinates on silence or
# near-silent / noisy audio (it was trained on YouTube subtitles). These strings
# are never legitimate dictation, so they are safe to strip. They appear in many
# forms and often run straight into real speech, so the patterns are surgical:
# the credit-run pattern only matches "Role [субтитров] И.Фамилия" (an initial +
# surname, which real sentence words never look like), so it can't eat real text.
_HALLUCINATIONS = [
    re.compile(r"\bDimaTorzok\b", re.IGNORECASE),
    re.compile(r"\bamara\.org\b", re.IGNORECASE),
    re.compile(r"\bпродолжение следует\b\.{0,3}", re.IGNORECASE),
    re.compile(r"\bспасибо за просмотр\b[^.!?\n]*[.!?]?", re.IGNORECASE),
    re.compile(r"\bподписыва[а-яё]*\b[^.!?\n]*[.!?]?", re.IGNORECASE),
    re.compile(r"\bспасибо за субтитры\b[^.!?\n]*[.!?]?", re.IGNORECASE),
    re.compile(r"\bдобавил[а-яё]*\s+субтитры\b", re.IGNORECASE),
    # "Автор/Редактор/Корректор [субтитров] И.Фамилия" runs (name = initial.Surname),
    # case-sensitive so a capitalized real word ("Посмотри") is NOT taken as a name.
    re.compile(
        r"(?:(?:Автор|Авторы|Редактор|Корректор|Переводчик)\s+(?:субтитров\s+)?"
        r"[А-ЯЁA-Z]\.\s?[А-ЯЁA-Z][а-яёa-z]+\s*)+"
    ),
    re.compile(
        r"\bсубтитр[а-яё]*\s+(?:делал|сделал|создал|создавал|подготовил|правил|"
        r"редактировал|предоставлен\w*)\b[^.!?\n]*[.!?]?",
        re.IGNORECASE,
    ),
]


@dataclass(frozen=True)
class TranscriptionResult:
    """Both the model output and the text presented to the user.

    Keeping the raw value is intentional: post-processing must never make a
    dictated thought unrecoverable. ``warnings`` records suspicious patterns
    without silently deleting them.
    """

    raw_text: str
    text: str
    warnings: tuple[str, ...] = ()
    backend: str = "unknown"
    duration: float | None = None
    language: str | None = None
    avg_token_probability: float | None = None
    avg_logprob: float | None = None
    no_speech_probability: float | None = None
    audio_peak: float | None = None
    retried: bool = False


def _norm_word(word: str) -> str:
    return re.sub(r"[^\w\-]", "", word, flags=re.UNICODE).casefold()


def _collapse_repetition(text: str, min_block: int = 3, min_repeats: int = 3) -> str:
    """Collapse a degenerate repetition loop at the end of a transcription.

    When Whisper's decoder degenerates it emits the same phrase over and over to
    the end of the audio, swallowing whatever was really said. A block of 3+ words
    repeated 3+ times back-to-back is never real dictation, so keep one copy."""
    words = text.split()
    for block in range(min_block, len(words) // min_repeats + 1):
        tail = [_norm_word(w) for w in words[-block:]]
        if not any(tail):  # punctuation-only block
            continue
        repeats = 1
        while (repeats + 1) * block <= len(words):
            prev = words[-(repeats + 1) * block : -repeats * block]
            if [_norm_word(w) for w in prev] != tail:
                break
            repeats += 1
        if repeats >= min_repeats:
            return " ".join(words[: len(words) - block * (repeats - 1)])
    return text


def _has_repetition_loop(text: str) -> bool:
    return _collapse_repetition(text) != text


class Transcriber:
    def __init__(self, server, smart_cleanup: bool = True, vocabulary=None) -> None:
        self.server = server
        self.smart_cleanup = smart_cleanup
        self.vocabulary = vocabulary

    def transcribe(self, audio_path, context: str | None = None) -> str:
        return self.transcribe_result(audio_path, context=context).text

    def transcribe_result(self, audio_path, context: str | None = None) -> TranscriptionResult:
        detailed = None
        if hasattr(self.server, "transcribe_detailed"):
            detailed = self.server.transcribe_detailed(audio_path)
            raw_text = detailed.text
        else:
            raw_text = self.server.transcribe(audio_path)
        return self.finalize_raw(raw_text, detailed, context=context)

    def finalize_raw(self, raw_text: str, detailed=None, context: str | None = None) -> TranscriptionResult:
        """Постобработка уже полученного текста (очистка, фильтры, словарь).

        Используется и обычным путём, и живой чанковой расшифровкой, где сырой
        текст собирается из кусков, расшифрованных ещё во время записи."""
        text = raw_text
        if self.smart_cleanup:
            text = self._clean(text)
        warnings: list[str] = []
        audio_peak = getattr(detailed, "audio_peak", None)
        no_speech = getattr(detailed, "no_speech_probability", None)
        if self._contains_hallucination_marker(text):
            warnings.append("possible_whisper_hallucination")
            # Subtitle-credit phrases are never legitimate dictation -- cut them
            # out even when they run into real speech. (The 2026-07-10 rewrite
            # lost this call and "Продолжение следует..." reached the clipboard.)
            text = self._strip_hallucinations(text)
            if not text:
                warnings.append("hallucination_suppressed")
            elif (audio_peak is not None and audio_peak < 0.006) or (
                no_speech is not None and no_speech >= 0.80
            ):
                # Whatever survived on silent audio is still model fantasy.
                # raw_text remains available in JSONL.
                text = ""
                warnings.append("hallucination_suppressed_on_silence")
        elif text and audio_peak is not None and audio_peak < 0.006:
            # The recorder uses the same threshold to classify a whole waveform
            # as room noise. Any confident-looking text from such a file is still
            # a model hallucination, even if it is not in the known phrase list.
            text = ""
            warnings.append("non_speech_transcript_suppressed")
        if _has_repetition_loop(text):
            warnings.append("possible_repetition_loop")
        if self.vocabulary is not None:
            text = self.vocabulary.apply(text, context=context)
        return TranscriptionResult(
            raw_text=raw_text,
            text=text,
            warnings=tuple(warnings),
            backend=getattr(detailed, "backend", "unknown"),
            duration=getattr(detailed, "duration", None),
            language=getattr(detailed, "language", None),
            avg_token_probability=getattr(detailed, "avg_token_probability", None),
            avg_logprob=getattr(detailed, "avg_logprob", None),
            no_speech_probability=getattr(detailed, "no_speech_probability", None),
            audio_peak=getattr(detailed, "audio_peak", None),
            retried=bool(getattr(detailed, "retried", False)),
        )

    @staticmethod
    def _contains_hallucination_marker(text: str) -> bool:
        return any(pattern.search(text) for pattern in _HALLUCINATIONS)

    def _strip_hallucinations(self, text: str) -> str:
        """Legacy helper kept for explicit/manual recovery workflows only.

        Normal dictation no longer calls this method because several markers
        (for example, "подписывай" or "спасибо за просмотр") are valid Russian
        phrases and the old implementation could erase an entire sentence.
        """
        for pattern in _HALLUCINATIONS:
            text = pattern.sub("", text)
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"^\s*[,.!?;:—-]+\s*", "", text)  # leftover leading punctuation
        return text.strip()

    def _clean(self, text: str) -> str:
        text = text.strip()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\s+([,.!?;:%])", r"\1", text)
        text = re.sub(r"([¿¡])\s+", r"\1", text)
        text = re.sub(r"\s+([»”])", r"\1", text)
        text = re.sub(r"([«“])\s+", r"\1", text)
        return text
