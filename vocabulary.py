from __future__ import annotations

import re
from pathlib import Path

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


class Vocabulary:
    """Loads a user-editable replacement dictionary and applies it to output."""

    def __init__(self, path: Path, context_dir: Path | None = None) -> None:
        self.path = path
        self.context_dir = context_dir
        self._contexts: dict[str, Vocabulary] = {}
        self.rules: list[tuple[re.Pattern, str]] = []
        self.terms: list[str] = []
        self._mtime = 0.0
        self.load()

    def ensure_file(self) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(DEFAULT_DICTIONARY, encoding="utf-8")
            self.path.chmod(0o600)

    def load(self) -> None:
        self.ensure_file()
        entries: list[tuple[str, str]] = []  # (variant, target)
        self.terms = []
        try:
            self._mtime = self.path.stat().st_mtime
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
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
            if self.path.stat().st_mtime != self._mtime:
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
        if path is None or not path.exists():
            return None
        cached = self._contexts.get(context)
        if cached is None or cached.path != path:
            cached = Vocabulary(path)
            self._contexts[context] = cached
        return cached
