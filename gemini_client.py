from __future__ import annotations

import http.client
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

# Text-only Gemini client for the cleanup pass (port of macbook/Sources/GeminiClient.swift).
# Only the finished whisper transcript is sent; audio never leaves the Mac.
#
# Free-tier keys hit 429 quickly, so every (model, key) pair has its own cooldown and a
# request walks the pairs: next key first, then the next model (separate quota). A model
# answering 404 / "no longer available" is skipped for the rest of the session. One hard
# deadline covers the whole walk: when it passes, the caller keeps the raw text.

HOST = "generativelanguage.googleapis.com"
ENV_FILE = Path.home() / ".config/whisper-dictate/env"


class GeminiError(RuntimeError):
    pass


def load_keys(env_file: Path = ENV_FILE) -> list[str]:
    """GEMINI_API_KEY / GEMINI_API_KEYS from the environment or ~/.config/whisper-dictate/env.
    Several keys may be separated by commas or spaces. Values are never logged."""
    raw: list[str] = [os.environ[n] for n in ("GEMINI_API_KEY", "GEMINI_API_KEYS") if os.environ.get(n)]
    if not raw:
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in lines:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("export "):
                s = s[7:]
            name, sep, value = s.partition("=")
            if sep and name.strip() in ("GEMINI_API_KEY", "GEMINI_API_KEYS"):
                raw.append(value.strip().strip("\"'"))
    keys: list[str] = []
    for chunk in raw:
        for k in re.split(r"[,\s;]+", chunk):
            if k and k not in keys:
                keys.append(k)
    return keys


def _model_unavailable(message: str) -> bool:
    m = message.lower()
    return any(s in m for s in ("no longer available", "is not found", "not found for api",
                                "is not supported for generatecontent", "model not found"))


class _Attempt:
    def __init__(self, model: str, idx: int) -> None:
        self.model = model
        self.idx = idx
        self.pair = f"{model}#{idx}"
        self.started = time.monotonic()


class GeminiTextClient:
    """Bookkeeping (cooldowns, bans, round robin) happens only in the caller's thread;
    worker threads just do HTTP over pooled keep-alive connections."""

    def __init__(self, keys: list[str] | None = None, log=None, hedge_after: float = 1.0) -> None:
        self.keys = list(keys) if keys is not None else load_keys()
        self.hedge_after = hedge_after
        self._log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self._lock = threading.Lock()          # one generate() at a time
        self._pool_lock = threading.Lock()
        self._idle: list[tuple[http.client.HTTPSConnection, float]] = []
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="gemini")
        self._round_robin = 0
        self._cooldown: dict[str, float] = {}  # "model#idx" -> monotonic deadline
        self._banned: dict[int, float] = {}    # key idx -> monotonic deadline
        self._missing_models: set[str] = set()
        self._no_thinking: set[str] = set()
        self._ssl = ssl.create_default_context()

    # --- connection pool -----------------------------------------------------------
    def _checkout(self, timeout: float) -> http.client.HTTPSConnection:
        now = time.monotonic()
        with self._pool_lock:
            while self._idle:
                conn, used = self._idle.pop()
                if now - used < 120:  # Google drops idle connections after a few minutes
                    conn.timeout = timeout
                    if conn.sock is not None:
                        conn.sock.settimeout(timeout)
                    return conn
                conn.close()
        conn = http.client.HTTPSConnection(HOST, timeout=timeout, context=self._ssl)
        conn.connect()
        return conn

    def _checkin(self, conn: http.client.HTTPSConnection) -> None:
        with self._pool_lock:
            if len(self._idle) < 3:
                self._idle.append((conn, time.monotonic()))
                return
        conn.close()

    def warmup(self, connections: int = 2) -> None:
        """Open DNS + TCP + TLS while the user is still talking (2 sockets: one spare
        for a hedged request)."""
        if not self.keys:
            return
        with self._pool_lock:
            missing = max(0, connections - len(self._idle))
        for _ in range(missing):
            try:
                self._checkin(self._checkout(timeout=5.0))
            except Exception as exc:
                self._log(f"gemini: warmup failed ({exc})")
                return

    # --- request ---------------------------------------------------------------
    def _thinking_config(self, model: str) -> dict | None:
        if model in self._no_thinking or "-pro" in model:
            return None
        if model.startswith("gemini-3"):
            return {"thinkingLevel": "minimal"}
        return {"thinkingBudget": 0}

    def _body(self, model: str, system: str, text: str) -> bytes:
        generation: dict = {"temperature": 0, "maxOutputTokens": 8192}
        thinking = self._thinking_config(model)
        if thinking:
            generation["thinkingConfig"] = thinking
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": generation,
            "safetySettings": [
                {"category": c, "threshold": "BLOCK_NONE"}
                for c in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                          "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
            ],
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _post(self, model: str, key: str, body: bytes, timeout: float) -> tuple[int, dict]:
        """Runs in a worker thread."""
        path = f"/v1beta/models/{model}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": key}
        for attempt in (1, 2):
            conn = self._checkout(timeout)
            try:
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                status = resp.status
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                    http.client.CannotSendRequest, http.client.BadStatusLine):
                conn.close()  # stale pooled connection: reconnect once
                if attempt == 2:
                    raise
                continue
            except Exception:
                conn.close()
                raise
            if resp.getheader("connection", "").lower() == "close":
                conn.close()
            else:
                self._checkin(conn)
            break
        try:
            parsed = json.loads(data.decode("utf-8", errors="replace")) if data else {}
        except json.JSONDecodeError:
            parsed = {}
        return status, parsed if isinstance(parsed, dict) else {}

    def _pick(self, models: list[str], tried: set[str], avoid_key: int | None = None,
              avoid_model: str | None = None) -> _Attempt | None:
        """Next usable (model, key). For a hedge, another model is preferred: slowness
        is correlated within one model's serving pool, not per key."""
        if avoid_model is not None:
            others = [m for m in models if m != avoid_model]
            att = self._pick(others, tried, avoid_key=avoid_key)
            if att is not None:
                return att
        now = time.monotonic()
        n = len(self.keys)
        for model in models:
            if model in self._missing_models:
                continue
            for off in range(n):
                idx = (self._round_robin + off) % n
                pair = f"{model}#{idx}"
                if idx == avoid_key or pair in tried:
                    continue
                if self._banned.get(idx, 0) > now or self._cooldown.get(pair, 0) > now:
                    continue
                return _Attempt(model, idx)
        return None

    def generate(self, system: str, text: str, models: list[str], deadline_seconds: float) -> tuple[str, str]:
        """Return (text, model). Raises GeminiError when nothing answered in time.

        Hedging: if the first request has not answered after `hedge_after` seconds, a
        second one goes out on another key; the first good answer wins. Gemini's latency
        is usually 0.6-1.2 s with rare 4-8 s spikes; the hedge cuts those."""
        if not self.keys:
            raise GeminiError("no Gemini keys")
        deadline = time.monotonic() + deadline_seconds
        with self._lock:
            tried: set[str] = set()
            in_flight: dict = {}
            hedged = False
            last_error = "rate limited"

            def launch(att: _Attempt) -> None:
                tried.add(att.pair)
                remaining = max(0.3, deadline - time.monotonic())
                fut = self._executor.submit(self._post, att.model, self.keys[att.idx],
                                            self._body(att.model, system, text), remaining)
                in_flight[fut] = att

            while True:
                if not in_flight:
                    att = self._pick(models, tried)
                    if att is None:
                        raise GeminiError(last_error)
                    if deadline - time.monotonic() < 0.3:
                        raise GeminiError("deadline")
                    launch(att)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GeminiError(f"deadline {deadline_seconds:.1f}s")
                wait_for = remaining
                if not hedged and len(in_flight) == 1:
                    first = next(iter(in_flight.values()))
                    wait_for = min(remaining, max(0.0, first.started + self.hedge_after - time.monotonic()))
                done, _ = wait(list(in_flight), timeout=wait_for, return_when=FIRST_COMPLETED)
                if not done:
                    if not hedged and time.monotonic() < deadline - 0.3:
                        busy = next(iter(in_flight.values()))
                        # Hedge only on the cheap model (flash-lite, 500/day free) with another
                        # key: never spend the quality model's 20/day on a duplicate.
                        cheap = next((m for m in models if "lite" in m), None)
                        second = self._pick([cheap], tried, avoid_key=busy.idx) if cheap else None
                        hedged = True
                        if second is not None:
                            self._log(f"gemini: no answer after {self.hedge_after:.1f}s, hedging on "
                                      f"{second.model} key#{second.idx + 1}")
                            launch(second)
                    continue
                for fut in done:
                    att = in_flight.pop(fut)
                    outcome = self._outcome(att, fut)
                    if outcome[0] == "ok":
                        self._round_robin = (att.idx + 1) % len(self.keys)
                        return outcome[1], att.model
                    if outcome[0] == "fatal" and not in_flight:
                        raise GeminiError(outcome[1])
                    if outcome[0] == "retry_same":
                        tried.discard(att.pair)
                    last_error = outcome[1]

    def _outcome(self, att: _Attempt, fut) -> tuple[str, str]:
        """('ok', text) | ('retry', why) | ('retry_same', why) | ('fatal', why). Caller thread."""
        tag = f"{att.model} key#{att.idx + 1}"
        try:
            status, payload = fut.result()
        except (socket.timeout, TimeoutError):
            return "fatal", f"timeout after {time.monotonic() - att.started:.1f}s"
        except OSError as exc:
            return "fatal", f"network: {exc}"
        err = payload.get("error") or {}
        message = str(err.get("message") or "")
        if status == 200:
            out, finish = self._parse(payload)
            if not out and finish not in (None, "STOP"):
                return "retry", f"blocked ({finish})"
            return "ok", out
        if status == 429:
            wait_s = self._retry_delay(err) or 30.0
            if "PerDay" in json.dumps(err):
                wait_s = max(wait_s, 3600.0)
            self._cooldown[att.pair] = time.monotonic() + wait_s
            self._log(f"gemini {tag}: 429, cooldown {wait_s:.0f}s")
            return "retry", "rate limited"
        if status == 400 and "thinking" in message.lower() and att.model not in self._no_thinking:
            self._no_thinking.add(att.model)
            self._log(f"gemini {tag}: thinking config rejected, retrying without")
            return "retry_same", "thinking config"
        if status == 404 or (status in (400, 403) and _model_unavailable(message)):
            self._missing_models.add(att.model)
            self._log(f"gemini {att.model}: unavailable ({status}), next model")
            return "retry", f"{status} {message[:80]}"
        if status in (401, 403) or (status == 400 and "api key" in message.lower()):
            self._banned[att.idx] = time.monotonic() + 6 * 3600
            self._log(f"gemini key#{att.idx + 1}: rejected ({status}), disabled for 6h")
            return "retry", "bad key"
        if status >= 500:
            self._log(f"gemini {tag}: {status}")
            return "retry", f"{status}"
        return "fatal", f"{status} {message[:120]}"

    @staticmethod
    def _parse(payload: dict) -> tuple[str, str | None]:
        candidates = payload.get("candidates") or []
        if not candidates:
            return "", (payload.get("promptFeedback") or {}).get("blockReason")
        first = candidates[0]
        parts = (first.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return text, first.get("finishReason")

    @staticmethod
    def _retry_delay(err: dict) -> float | None:
        for d in err.get("details") or []:
            value = d.get("retryDelay") if isinstance(d, dict) else None
            if isinstance(value, str):
                try:
                    return max(1.0, float(value.rstrip("s")))
                except ValueError:
                    pass
        return None
