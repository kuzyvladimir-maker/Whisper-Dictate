from __future__ import annotations

import re
from dataclasses import dataclass

# Subtitle-credit artifacts Whisper's Russian model hallucinates on silence or
# near-silent / noisy audio (it was trained on YouTube subtitles).
#
# Two groups, because several markers are also ordinary Russian:
# - UNAMBIGUOUS: never legitimate dictation, cut out always, even mid-speech.
# - AMBIGUOUS: "подписывай", "спасибо за просмотр", "продолжение следует", credit
#   runs. Real phrases ("Подписывай договор с Walmart...") must survive, so these
#   are removed only when the audio is silent, or when a whole sentence is
#   nothing but the marker (Whisper appends "Продолжение следует..." as its own
#   sentence after real speech).
_UNAMBIGUOUS_HALLUCINATIONS = [
    re.compile(r"\bDimaTorzok\b", re.IGNORECASE),
    re.compile(r"\bamara\.org\b", re.IGNORECASE),
    re.compile(
        r"\bсубтитр[а-яё]*\s+(?:делал|сделал|создал|создавал|подготовил|правил|"
        r"редактировал|предоставлен\w*)\b[^.!?\n]*[.!?]?",
        re.IGNORECASE,
    ),
]
_AMBIGUOUS_HALLUCINATIONS = [
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
]
_HALLUCINATIONS = _UNAMBIGUOUS_HALLUCINATIONS + _AMBIGUOUS_HALLUCINATIONS

# A sentence that is ONLY an ambiguous marker (anchored, whole sentence).
_MARKER_ONLY_SENTENCES = [
    re.compile(r"^продолжение следует[.…!]*$", re.IGNORECASE),
    re.compile(r"^спасибо за (?:просмотр|внимание и просмотр|субтитры)[.…!]*$", re.IGNORECASE),
    re.compile(
        r"^(?:ставьте лайки?[,]? )?(?:и )?подписыва[а-яё]*(?: на (?:наш |мой )?канал[а-яё]*)?"
        r"(?:[,]? (?:и )?ставьте лайки?)?[.…!]*$",
        re.IGNORECASE,
    ),
    re.compile(r"^добавил[а-яё]*\s+субтитры[.…!]*$", re.IGNORECASE),
    re.compile(
        r"^(?:(?:Автор|Авторы|Редактор|Корректор|Переводчик)\s+(?:субтитров\s+)?"
        r"[А-ЯЁA-Z]\.\s?[А-ЯЁA-Z][а-яёa-z]+\s*)+[.…!]*$"
    ),
]
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


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
            silent = (audio_peak is not None and audio_peak < 0.006) or (
                no_speech is not None and no_speech >= 0.80
            )
            if silent:
                # Marker on silent audio: the whole result is model fantasy.
                # raw_text remains available in JSONL.
                text = ""
                warnings.append("hallucination_suppressed_on_silence")
            else:
                # Real speech: cut only unambiguous credits and sentences that are
                # nothing but a marker. "Подписывай договор..." stays intact.
                text = self._strip_unambiguous(text)
                text = self._drop_marker_only_sentences(text)
                if not text:
                    warnings.append("hallucination_suppressed")
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

    @staticmethod
    def _tidy(text: str) -> str:
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"^\s*[,.!?;:—-]+\s*", "", text)  # leftover leading punctuation
        return text.strip()

    def _strip_unambiguous(self, text: str) -> str:
        for pattern in _UNAMBIGUOUS_HALLUCINATIONS:
            text = pattern.sub("", text)
        return self._tidy(text)

    def _drop_marker_only_sentences(self, text: str) -> str:
        kept = [
            sentence for sentence in _SENTENCE_SPLIT.split(text)
            if not any(p.match(sentence.strip()) for p in _MARKER_ONLY_SENTENCES)
        ]
        return self._tidy(" ".join(kept))

    def _strip_hallucinations(self, text: str) -> str:
        """Strip every marker, ambiguous ones included. Destructive: "Подписывай
        договор..." loses the whole sentence. Kept for manual recovery only;
        finalize_raw uses _strip_unambiguous + _drop_marker_only_sentences."""
        for pattern in _HALLUCINATIONS:
            text = pattern.sub("", text)
        return self._tidy(text)

    def _clean(self, text: str) -> str:
        text = text.strip()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\s+([,.!?;:%])", r"\1", text)
        text = re.sub(r"([¿¡])\s+", r"\1", text)
        text = re.sub(r"\s+([»”])", r"\1", text)
        text = re.sub(r"([«“])\s+", r"\1", text)
        return text
