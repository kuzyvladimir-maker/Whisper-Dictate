from __future__ import annotations

import time
import unittest

from gemini_client import GeminiError, GeminiTextClient


def ok(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}


class ScriptedClient(GeminiTextClient):
    """_post replaced by a script: key -> (delay, status, payload)."""

    def __init__(self, script: dict, **kw) -> None:
        super().__init__(keys=list(script.keys()), log=lambda _m: None, **kw)
        self.script = script
        self.calls: list[tuple[str, str]] = []

    def _post(self, model, key, body, timeout):
        self.calls.append((model, key))
        delay, status, payload = self.script[key](model) if callable(self.script[key]) else self.script[key]
        time.sleep(min(delay, timeout))
        return status, payload


class GeminiClientTests(unittest.TestCase):
    def test_hedge_wins_when_first_request_is_slow(self) -> None:
        c = ScriptedClient({"k1": (3.0, 200, ok("slow")), "k2": (0.05, 200, ok("fast"))}, hedge_after=0.3)
        t0 = time.monotonic()
        text, _ = c.generate("s", "t", ["m-lite"], deadline_seconds=4)
        self.assertEqual(text, "fast")
        self.assertLess(time.monotonic() - t0, 2.0)  # slow request takes 3 s

    def test_429_moves_to_next_key(self) -> None:
        c = ScriptedClient({"k1": (0, 429, {"error": {"message": "quota"}}), "k2": (0, 200, ok("ok"))})
        self.assertEqual(c.generate("s", "t", ["m"], 4)[0], "ok")
        self.assertEqual([k for _, k in c.calls], ["k1", "k2"])

    def test_404_moves_to_next_model(self) -> None:
        def answer(model):
            if model == "old":
                return (0, 404, {"error": {"message": "models/old is no longer available to new users"}})
            return (0, 200, ok("new"))
        c = ScriptedClient({"k1": answer})
        text, model = c.generate("s", "t", ["old", "new"], 4)
        self.assertEqual((text, model), ("new", "new"))
        self.assertIn("old", c._missing_models)

    def test_deadline_raises(self) -> None:
        c = ScriptedClient({"k1": (2.0, 200, ok("late"))}, hedge_after=5)
        with self.assertRaises(GeminiError):
            c.generate("s", "t", ["m"], deadline_seconds=0.5)


if __name__ == "__main__":
    unittest.main()
