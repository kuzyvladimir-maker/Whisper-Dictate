from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


class WhisperServerError(RuntimeError):
    pass


class WhisperServer:
    """Spawns whisper-server as a child process and exposes a simple transcribe() method."""

    def __init__(
        self,
        binary: Path,
        model_path: Path,
        language: str = "auto",
        initial_prompt: str = "",
        startup_timeout: float = 30.0,
    ) -> None:
        self.binary = binary
        self.model_path = model_path
        self.language = language
        self.initial_prompt = initial_prompt.strip()
        self.startup_timeout = startup_timeout
        self._process: subprocess.Popen | None = None
        self._port: int | None = None

    @property
    def url(self) -> str:
        if self._port is None:
            raise WhisperServerError("Server is not running")
        return f"http://127.0.0.1:{self._port}/inference"

    def start(self) -> None:
        if not self.binary.exists():
            raise FileNotFoundError(f"whisper-server binary not found: {self.binary}")
        if not self.model_path.exists():
            raise FileNotFoundError(f"Whisper model not found: {self.model_path}")

        self._port = _pick_free_port()
        args = [
            str(self.binary),
            "-m", str(self.model_path),
            "--host", "127.0.0.1",
            "--port", str(self._port),
            "-l", self.language,
            "--no-timestamps",
        ]
        # initial_prompt is passed per-request in transcribe(), not at server startup,
        # so the encoder doesn't carry it through every inference.

        self._process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        self._wait_until_ready()

    def stop(self) -> None:
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None
        self._port = None

    def transcribe(self, audio_path: Path) -> str:
        if self._port is None:
            raise WhisperServerError("Server is not running")

        fields = {
            "response_format": "json",
            "language": self.language,
        }
        if self.initial_prompt:
            fields["prompt"] = self.initial_prompt

        boundary = uuid.uuid4().hex
        body = _build_multipart(
            boundary,
            fields=fields,
            file_path=audio_path,
            file_field="file",
        )
        req = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise WhisperServerError(f"whisper-server request failed: {exc}") from exc

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise WhisperServerError(f"whisper-server returned invalid JSON: {payload[:200]}") from exc

        return (data.get("text") or "").strip()

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._process is None or self._process.poll() is not None:
                raise WhisperServerError("whisper-server exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.2)
        self.stop()
        raise WhisperServerError("whisper-server did not become ready in time")


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]




def _build_multipart(boundary: str, fields: dict[str, str], file_path: Path, file_field: str) -> bytes:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(value.encode("utf-8"))
        parts.append(b"\r\n")

    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'.encode()
    )
    parts.append(b"Content-Type: audio/wav\r\n\r\n")
    parts.append(file_path.read_bytes())
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)
