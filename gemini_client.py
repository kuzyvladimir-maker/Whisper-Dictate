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


class GeminiTextClient:
    def __init__(self, keys: list[str] | None = None, log=None) -> None:
        self.keys = list(keys) if keys is not None else load_keys()
        self._log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self._lock = threading.Lock()          # one request at a time over one connection
        self._conn: http.client.HTTPSConnection | None = None
        self._conn_used_at = 0.0
        self._round_robin = 0
        self._cooldown: dict[str, float] = {}  # "model#idx" -> monotonic deadline
        self._banned: dict[int, float] = {}    # key idx -> monotonic deadline
        self._missing_models: set[str] = set()
        self._no_thinking: set[str] = set()
        self._ssl = ssl.create_default_context()

    # --- connection ------------------------------------------------------------
    def _connection(self, timeout: float) -> http.client.HTTPSConnection:
        # Google closes idle connections after a few minutes; reconnect pre-emptively.
        if self._conn is not None and time.monotonic() - self._conn_used_at > 120:
            self._drop_connection()
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(HOST, timeout=timeout, context=self._ssl)
            self._conn.connect()
        else:
            self._conn.timeout = timeout
            if self._conn.sock is not None:
                self._conn.sock.settimeout(timeout)
        self._conn_used_at = time.monotonic()
        return self._conn

    def _drop_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def warmup(self) -> None:
        """Open DNS + TCP + TLS while the user is still talking."""
        if not self.keys:
            return
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._connection(timeout=5.0)
        except Exception as exc:
            self._drop_connection()
            self._log(f"gemini: warmup failed ({exc})")
        finally:
            self._lock.release()

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
        path = f"/v1beta/models/{model}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": key}
        for attempt in (1, 2):
            conn = self._connection(timeout)
            try:
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                status = resp.status
                if resp.getheader("connection", "").lower() == "close":
                    self._drop_connection()
                break
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                    http.client.CannotSendRequest, http.client.BadStatusLine):
                # Stale pooled connection: reconnect once.
                self._drop_connection()
                if attempt == 2:
                    raise
            except Exception:
                self._drop_connection()
                raise
        try:
            parsed = json.loads(data.decode("utf-8", errors="replace")) if data else {}
        except json.JSONDecodeError:
            parsed = {}
        return status, parsed if isinstance(parsed, dict) else {}

    def generate(self, system: str, text: str, models: list[str], deadline_seconds: float) -> tuple[str, str]:
        """Return (text, model). Raises GeminiError when nothing answered in time."""
        if not self.keys:
            raise GeminiError("no Gemini keys")
        deadline = time.monotonic() + deadline_seconds
        with self._lock:
            tried: set[str] = set()
            last_error = "rate limited"
            n = len(self.keys)
            while True:
                now = time.monotonic()
                pick = None
                for model in models:
                    if model in self._missing_models:
                        continue
                    for off in range(n):
                        idx = (self._round_robin + off) % n
                        pair = f"{model}#{idx}"
                        if pair in tried or self._banned.get(idx, 0) > now or self._cooldown.get(pair, 0) > now:
                            continue
                        pick = (model, idx, pair)
                        break
                    if pick:
                        break
                if pick is None:
                    raise GeminiError(last_error)
                model, idx, pair = pick
                remaining = deadline - time.monotonic()
                if remaining < 0.3:
                    raise GeminiError("deadline")
                tried.add(pair)
                t0 = time.monotonic()
                try:
                    status, payload = self._post(model, self.keys[idx], self._body(model, system, text), remaining)
                except (socket.timeout, TimeoutError) as exc:
                    raise GeminiError(f"timeout after {time.monotonic() - t0:.1f}s") from exc
                except OSError as exc:
                    raise GeminiError(f"network: {exc}") from exc
                err = payload.get("error") or {}
                message = str(err.get("message") or "")
                tag = f"{model} key#{idx + 1}"
                if status == 200:
                    out, finish = self._parse(payload)
                    self._round_robin = (idx + 1) % n
                    if not out and finish not in (None, "STOP"):
                        last_error = f"blocked ({finish})"
                        continue
                    return out, model
                if status == 429:
                    wait = self._retry_delay(err) or 30.0
                    if "PerDay" in json.dumps(err):
                        wait = max(wait, 3600.0)
                    self._cooldown[pair] = time.monotonic() + wait
                    self._log(f"gemini {tag}: 429, cooldown {wait:.0f}s")
                    last_error = "rate limited"
                    continue
                if status == 400 and "thinking" in message.lower() and model not in self._no_thinking:
                    self._no_thinking.add(model)
                    tried.discard(pair)
                    self._log(f"gemini {tag}: thinking config rejected, retrying without")
                    continue
                if status == 404 or (status in (400, 403) and _model_unavailable(message)):
                    self._missing_models.add(model)
                    self._log(f"gemini {model}: unavailable ({status}), next model")
                    last_error = f"{status} {message[:80]}"
                    continue
                if status in (401, 403) or (status == 400 and "api key" in message.lower()):
                    self._banned[idx] = time.monotonic() + 6 * 3600
                    self._log(f"gemini key#{idx + 1}: rejected ({status}), disabled for 6h")
                    last_error = "bad key"
                    continue
                if status >= 500:
                    self._log(f"gemini {tag}: {status}")
                    last_error = f"{status}"
                    continue
                raise GeminiError(f"{status} {message[:120]}")

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
