from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from main import WhisperDictateApp, _atomic_write_json


class ConfigTests(unittest.TestCase):
    def test_user_config_merges_over_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            default = root / "config.json"
            user = root / "config.local.json"
            default.write_text(json.dumps({"language": "ru", "sample_rate": 16000}), encoding="utf-8")
            user.write_text(json.dumps({"language": "en"}), encoding="utf-8")
            with patch.dict(main.PATHS, {"default_config": default, "user_config": user}):
                config = WhisperDictateApp.__new__(WhisperDictateApp)._load_config()
            self.assertEqual(config["language"], "en")
            self.assertEqual(config["sample_rate"], 16000)
            self.assertIn("persistent_server", config)

    def test_invalid_user_config_does_not_break_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            default = root / "config.json"
            user = root / "config.local.json"
            default.write_text(json.dumps({"language": "ru"}), encoding="utf-8")
            user.write_text("{broken", encoding="utf-8")
            with patch.dict(main.PATHS, {"default_config": default, "user_config": user}):
                config = WhisperDictateApp.__new__(WhisperDictateApp)._load_config()
            self.assertEqual(config["language"], "ru")

    def test_atomic_config_write_uses_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            _atomic_write_json(path, {"language": "ru"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"language": "ru"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
