from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vocabulary import Vocabulary


class VocabularyTests(unittest.TestCase):
    def test_bare_line_is_a_term_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictionary.txt"
            path.write_text("вику | вико = Veeqo\nИван Петров\n", encoding="utf-8")
            v = Vocabulary(path)
            self.assertEqual(v.terms, ["Veeqo", "Иван Петров"])
            self.assertEqual(v.apply("проверь вику"), "проверь Veeqo")

    def test_evicted_master_uses_cache_and_is_not_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            master = root / "icloud" / "dictionary.txt"
            cache = root / "dictionary.cache.txt"
            master.parent.mkdir()
            master.write_text("вику = Veeqo\n", encoding="utf-8")
            Vocabulary(master, cache_path=cache)  # first read fills the cache
            self.assertEqual(cache.read_text(encoding="utf-8"), "вику = Veeqo\n")
            # iCloud evicts the file: only a placeholder remains.
            master.unlink()
            (master.parent / ".dictionary.txt.icloud").write_text("", encoding="utf-8")
            with mock.patch("vocabulary.subprocess.Popen"):
                v = Vocabulary(master, cache_path=cache)
            self.assertFalse(master.exists())  # never recreated over the evicted copy
            self.assertEqual(v.apply("вику"), "Veeqo")

    def test_add_line_appends_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictionary.txt"
            path.write_text("# header\n", encoding="utf-8")
            v = Vocabulary(path)
            v.add_line("Salutem Vita\n")
            v.add_line("Salutem Vita")
            v.add_line("стар фит = STARFIT")
            self.assertEqual(path.read_text(encoding="utf-8"), "# header\nSalutem Vita\nстар фит = STARFIT\n")
            self.assertEqual(v.apply("купи стар фит"), "купи STARFIT")
            with self.assertRaises(ValueError):
                v.add_line("   ")


if __name__ == "__main__":
    unittest.main()
