from __future__ import annotations

import unittest

from cleaner import TextCleaner
from gemini_client import GeminiError


class FakeGemini:
    def __init__(self, reply: str | None = None, error: str | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[list[str]] = []

    def generate(self, system: str, text: str, models: list[str], deadline_seconds: float):
        self.calls.append(models)
        if self.error:
            raise GeminiError(self.error)
        return self.reply, models[0]

    def warmup(self) -> None:
        pass


LONG = " ".join(["слово"] * 70)
MEDIUM = "ну вот короче нам надо проверить листинги на Walmart сегодня вечером"


def cleaner_with(fake: FakeGemini, **config) -> TextCleaner:
    c = TextCleaner({"cleanup_backend": "gemini", **config})
    c._gemini_client = lambda: fake
    return c


class CleanerTests(unittest.TestCase):
    def test_short_phrase_skips_cleanup_in_normal_mode(self) -> None:
        fake = FakeGemini("x")
        text, used = cleaner_with(fake).clean("Привет, как дела?")
        self.assertEqual((text, used), ("Привет, как дела?", None))
        self.assertEqual(fake.calls, [])

    def test_short_phrase_is_cleaned_in_code_mode(self) -> None:
        fake = FakeGemini("Открой `main.py`.")
        text, used = cleaner_with(fake, cleanup_mode="code").clean("открой main точка пай")
        self.assertEqual(used, "gemini")
        self.assertEqual(text, "Открой `main.py`.")

    def test_model_routing(self) -> None:
        fake = FakeGemini(MEDIUM)
        c = cleaner_with(fake)
        c.clean(MEDIUM)
        self.assertEqual(fake.calls[-1][0], "gemini-3.5-flash-lite")
        fake.reply = LONG
        c.clean(LONG)
        self.assertEqual(fake.calls[-1][0], "gemini-3.6-flash")
        c.mode = "email"
        fake.reply = MEDIUM
        c.clean(MEDIUM)
        self.assertEqual(fake.calls[-1][0], "gemini-3.6-flash")

    def test_gemini_failure_keeps_raw_text(self) -> None:
        fake = FakeGemini(error="deadline")
        text, used = cleaner_with(fake).clean(MEDIUM)
        self.assertEqual((text, used), (MEDIUM, None))

    def test_insane_output_is_rejected(self) -> None:
        fake = FakeGemini("Ок.")
        text, used = cleaner_with(fake).clean(MEDIUM)
        self.assertEqual((text, used), (MEDIUM, None))


if __name__ == "__main__":
    unittest.main()
