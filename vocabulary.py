from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

# Shared master dictionary (iMac + MacBook) in iCloud Drive: private, synced by macOS.
ICLOUD_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/WhisperDictate"

# Seed dictionary written on first run. Lines: "variant1 | variant2 = Correct".
# Left side = how Whisper MIS-hears the term (case-insensitive, several variants
# via |, spaces match any whitespace). Right side = how it should be written.
DEFAULT_DICTIONARY = """\
# Словарь Whisper Dictate.
# Формат:  что_услышал | ещё_вариант = Как писать
# Слева — ошибочные варианты (регистр не важен), справа — правильное написание.
# Строки с # и пустые игнорируются. Сначала пишутся более длинные фразы.

клауд код | клод код | клауд-код | cloud code | клот код = Claude Code
клауд кода | клод кода = Claude Code
антропик | анфропик = Anthropic
чат джипити | чат гпт | чатгпт = ChatGPT
курсор айди = Cursor
клауд | клод | cloud = Claude
"""


def _icloud_placeholder(path: Path) -> Path:
    """An evicted iCloud file is replaced by '.<name>.icloud' next to it."""
    return path.with_name(f".{path.name}.icloud")


class Vocabulary:
    """Loads a user-editable replacement dictionary and applies it to output.

    Format (shared with macbook/ Lite):
        что_услышал | вариант = Как писать   -> replacement rule + term
        Как писать                          -> term only (spelling hint)

    ``path`` may live in iCloud Drive. ``cache_path`` keeps the last good copy so an
    evicted / not-yet-downloaded master never empties the dictionary, and a missing
    master is never recreated over an evicted one.
    """

    def __init__(
        self,
        path: Path,
        context_dir: Path | None = None,
        cache_path: Path | None = None,
        context_cache_dir: Path | None = None,
    ) -> None:
        self.path = path
        self.context_dir = context_dir
        self.cache_path = cache_path
        self.context_cache_dir = context_cache_dir
        self._contexts: dict[str, Vocabulary] = {}
        self.rules: list[tuple[re.Pattern, str]] = []
        self.terms: list[str] = []
        self._mtime = 0.0
        self._source: Path | None = None
        self.load()

    def ensure_file(self) -> None:
        if self.path.exists() or _icloud_placeholder(self.path).exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.cache_path is not None and self.cache_path.exists():
            shutil.copyfile(self.cache_path, self.path)
        else:
            self.path.write_text(DEFAULT_DICTIONARY, encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _resolve_source(self) -> Path | None:
        """Master if readable (refreshing the cache), else the cache."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            if _icloud_placeholder(self.path).exists():
                try:  # ask iCloud to bring the file back; the cache serves meanwhile
                    subprocess.Popen(["/usr/bin/brctl", "download", str(self.path)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    pass
            if self.cache_path is not None and self.cache_path.exists():
                return self.cache_path
            return None
        if self.cache_path is not None:
            try:
                if not self.cache_path.exists() or self.cache_path.read_text(encoding="utf-8") != text:
                    self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                    self.cache_path.write_text(text, encoding="utf-8")
                    self.cache_path.chmod(0o600)
            except OSError:
                pass
        return self.path

    def add_line(self, line: str) -> str:
        """Append `Term` or `услышал = Как писать` (menu: «Добавить слово из буфера»)."""
        line = (line or "").strip().splitlines()[0].strip() if (line or "").strip() else ""
        if not line or len(line) > 120 or line.startswith("#"):
            raise ValueError("в буфере нет подходящего слова (одна строка, до 120 символов)")
        self.ensure_file()
        target = self.path if self.path.exists() else (self.cache_path or self.path)
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            text = DEFAULT_DICTIONARY
        if line not in (existing.strip() for existing in text.splitlines()):
            if text and not text.endswith("\n"):
                text += "\n"
            text += line + "\n"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self.load()
        return line

    def load(self) -> None:
        self.ensure_file()
        entries: list[tuple[str, str]] = []  # (variant, target)
        self.terms = []
        self.rules = []
        source = self._resolve_source()
        self._source = source
        if source is None:
            return
        try:
            self._mtime = source.stat().st_mtime
            lines = source.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                self.terms.append(line)  # spelling hint only
                continue
            left, target = line.rsplit("=", 1)
            target = target.strip()
            if not target:
                continue
            self.terms.append(target)
            for variant in left.split("|"):
                variant = variant.strip()
                if variant:
                    entries.append((variant, target))

        # Longer variants first so phrases win over their constituent words.
        entries.sort(key=lambda e: len(e[0]), reverse=True)
        self.rules = []
        for variant, target in entries:
            pattern = r"\b" + r"\s+".join(re.escape(w) for w in variant.split()) + r"\b"
            try:
                self.rules.append((re.compile(pattern, re.IGNORECASE | re.UNICODE), target))
            except re.error:
                continue

    def _reload_if_changed(self) -> None:
        try:
            current = self.path.stat().st_mtime if self.path.exists() else None
        except OSError:
            current = None
        if self._source != self.path and current is not None:
            self.load()  # master came back (downloaded from iCloud)
            return
        try:
            source = self._source or self.path
            if source.stat().st_mtime != self._mtime:
                self.load()
        except OSError:
            pass

    def apply(self, text: str, context: str | None = None) -> str:
        # Pick up edits to the dictionary without restarting the app.
        self._reload_if_changed()
        for pattern, target in self.rules:
            text = pattern.sub(target, text)
        contextual = self._get_context(context)
        if contextual is not None:
            text = contextual.apply(text)
        return text

    def prompt(self, base: str = "", context: str | None = None) -> str:
        """Bias Whisper toward the correct spellings by listing them as context."""
        terms = list(self.terms)
        contextual = self._get_context(context)
        if contextual is not None:
            contextual._reload_if_changed()
            terms.extend(contextual.terms)
        if not terms:
            return base
        # Keep order, drop duplicates.
        seen, uniq = set(), []
        for t in terms:
            if t.lower() not in seen:
                seen.add(t.lower())
                uniq.append(t)
        listing = ", ".join(uniq)
        return f"{base} {listing}".strip() if base else listing

    def context_path(self, context: str) -> Path | None:
        if self.context_dir is None:
            return None
        key = re.sub(r"[^A-Za-z0-9_.-]+", "_", context).strip("._")[:120]
        if not key:
            return None
        return self.context_dir / f"{key}.txt"

    def ensure_context_file(self, context: str) -> Path | None:
        path = self.context_path(context)
        if path is None:
            return None
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_text(
                "# Словарь для приложения: " + context + "\n"
                "# Формат: что_услышал | вариант = Как писать\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
        self._contexts.pop(context, None)
        return path

    def _get_context(self, context: str | None) -> Vocabulary | None:
        if not context:
            return None
        path = self.context_path(context)
        if path is None:
            return None
        cache = self.context_cache_dir / path.name if self.context_cache_dir is not None else None
        if not (path.exists() or _icloud_placeholder(path).exists() or (cache is not None and cache.exists())):
            return None
        cached = self._contexts.get(context)
        if cached is None or cached.path != path:
            cache = self.context_cache_dir / path.name if self.context_cache_dir is not None else None
            cached = Vocabulary(path, cache_path=cache)
            self._contexts[context] = cached
        return cached

    def all_terms(self, context: str | None = None) -> list[str]:
        """Global + per-app terms, deduplicated (for the Gemini cleanup prompt)."""
        self._reload_if_changed()
        terms = list(self.terms)
        contextual = self._get_context(context)
        if contextual is not None:
            contextual._reload_if_changed()
            terms.extend(contextual.terms)
        seen: set[str] = set()
        out: list[str] = []
        for t in terms:
            if t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return out
