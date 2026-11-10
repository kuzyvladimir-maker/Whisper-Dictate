from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from transcriber import Transcriber
from vocabulary import Vocabulary
from whisper_server import WhisperResponse


class FakeEngine:
    def __init__(self, text: str) -> None:
        self.text = text

    def transcribe(self, _audio_path) -> str:
        return self.text


class DetailedFakeEngine:
    def __init__(self, response: WhisperResponse) -> None:
        self.response = response

    def transcribe_detailed(self, _audio_path) -> WhisperResponse:
        return self.response


class TranscriberTests(unittest.TestCase):
    def test_valid_phrases_are_never_deleted(self) -> None:
        phrases = [
            "Подписывай документ электронной подписью.",
            "Спасибо за просмотр документа, внеси правки.",
            "Продолжение следует отдельным файлом.",
            "Редактор субтитров И.Иванов подготовил пример.",
            "Подписывай договор с Walmart сегодня, а потом позвони мне.",
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                result = Transcriber(FakeEngine(phrase)).transcribe_result("unused.wav")
                self.assertEqual(result.text, phrase)
                self.assertEqual(result.raw_text, phrase)
                self.assertIn("possible_whisper_hallucination", result.warnings)

    def test_marker_only_sentences_and_credits_are_cut_from_real_speech(self) -> None:
        cases = {
            "Ну всё, до завтра. Продолжение следует...": "Ну всё, до завтра.",
            "Отправь отчёт. Подписывайтесь на канал!": "Отправь отчёт.",
            "Готово. Субтитры сделал DimaTorzok": "Готово.",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                result = Transcriber(FakeEngine(raw)).transcribe_result("unused.wav")
                self.assertEqual(result.text, expected)
                self.assertEqual(result.raw_text, raw)

    def test_marker_alone_is_suppressed(self) -> None:
        result = Transcriber(FakeEngine("Продолжение следует...")).transcribe_result("unused.wav")
        self.assertEqual(result.text, "")
        self.assertIn("hallucination_suppressed", result.warnings)

    def test_repetition_is_flagged_but_preserved(self) -> None:
        phrase = "проверь этот документ проверь этот документ проверь этот документ"
        result = Transcriber(FakeEngine(phrase)).transcribe_result("unused.wav")
        self.assertEqual(result.text, phrase)
        self.assertIn("possible_repetition_loop", result.warnings)

    def test_cleanup_and_vocabulary_are_non_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dictionary = Path(tmp) / "dictionary.txt"
            dictionary.write_text("чат джипити = ChatGPT\n", encoding="utf-8")
            vocabulary = Vocabulary(dictionary)
            result = Transcriber(
                FakeEngine("  Привет ,   чат джипити!  "),
                vocabulary=vocabulary,
            ).transcribe_result("unused.wav")
            self.assertEqual(result.raw_text, "  Привет ,   чат джипити!  ")
            self.assertEqual(result.text, "Привет, ChatGPT!")

    def test_hallucination_is_suppressed_only_with_silence_evidence(self) -> None:
        raw = "Спасибо за просмотр."
        response = WhisperResponse(raw, "server", audio_peak=0.001)
        result = Transcriber(DetailedFakeEngine(response)).transcribe_result("unused.wav")
        self.assertEqual(result.raw_text, raw)
        self.assertEqual(result.text, "")
        self.assertIn("hallucination_suppressed_on_silence", result.warnings)

    def test_unknown_text_from_silent_audio_is_also_suppressed(self) -> None:
        raw = "Неизвестная фраза, которой нет в списке."
        response = WhisperResponse(raw, "server", audio_peak=0.001)
        result = Transcriber(DetailedFakeEngine(response)).transcribe_result("unused.wav")
        self.assertEqual(result.raw_text, raw)
        self.assertEqual(result.text, "")
        self.assertIn("non_speech_transcript_suppressed", result.warnings)

    def test_context_dictionary_applies_only_to_matching_app(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vocabulary = Vocabulary(root / "dictionary.txt", context_dir=root / "dictionaries")
            context_path = vocabulary.ensure_context_file("com.microsoft.VSCode")
            assert context_path is not None
            context_path.write_text("ви эс код = VS Code\n", encoding="utf-8")
            self.assertEqual(
                vocabulary.apply("Открой ви эс код", context="com.microsoft.VSCode"),
                "Открой VS Code",
            )
            self.assertEqual(vocabulary.apply("Открой ви эс код", context="com.apple.TextEdit"), "Открой ви эс код")


if __name__ == "__main__":
    unittest.main()
