from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from array import array
from dataclasses import dataclass, replace
from pathlib import Path


class WhisperServerError(RuntimeError):
    pass


@dataclass(frozen=True)
class WhisperResponse:
    text: str
    backend: str
    duration: float | None = None
    language: str | None = None
    avg_token_probability: float | None = None
    avg_logprob: float | None = None
    no_speech_probability: float | None = None
    audio_peak: float | None = None
    retried: bool = False


def _wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() / wav.getframerate()
    except (OSError, wave.Error, ZeroDivisionError):
        return None


def _wav_peak(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as wav:
            if wav.getsampwidth() != 2:
                return None
            samples = array("h")
            samples.frombytes(wav.readframes(wav.getnframes()))
        if sys.byteorder != "little":
            samples.byteswap()
        return max((abs(sample) for sample in samples), default=0) / 32767.0
    except (OSError, wave.Error):
        return None


def _mean(values) -> float | None:
    numbers = [float(value) for value in values if value is not None]
    return sum(numbers) / len(numbers) if numbers else None


def _split_wav_on_silence(
    path: Path,
    tmp_dir: Path,
    min_seconds: float = 40.0,
    max_seconds: float = 75.0,
) -> list[Path]:
    """Cut a long mono 16-bit WAV into chunks at natural pauses.

    Whisper degrades on long single passes (drops middle sections, VAD loses
    ~1/3 of an 8-min file in A/B tests), while the same audio fed as ~40-75s
    pieces through the warm server came back 99.6% complete. Each cut lands on
    the quietest 50 ms window inside [start+min, start+max], which in real
    dictation is almost always a breath pause. Returns [] if the file can't be
    split (caller falls back to the single-pass path)."""
    try:
        import numpy as np

        with wave.open(str(path), "rb") as wav:
            if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
                return []
            rate = wav.getframerate()
            raw = wav.readframes(wav.getnframes())
        samples = np.abs(np.frombuffer(raw, dtype="<i2").astype(np.int32))
        win = max(1, int(rate * 0.05))
        n_win = len(samples) // win
        if n_win < 2:
            return []
        peaks = samples[: n_win * win].reshape(n_win, win).max(axis=1)

        bounds = [0]
        while True:
            remain = (len(samples) - bounds[-1]) / rate
            if remain <= max_seconds:
                bounds.append(len(samples))
                break
            lo = int((bounds[-1] / rate + min_seconds) / 0.05)
            hi = min(int((bounds[-1] / rate + max_seconds) / 0.05), n_win - 1)
            if hi <= lo:
                bounds.append(len(samples))
                break
            quietest = lo + int(np.argmin(peaks[lo:hi]))
            bounds.append(quietest * win + win // 2)
        if len(bounds) < 3:
            return []

        chunks: list[Path] = []
        for i in range(len(bounds) - 1):
            chunk_path = tmp_dir / f"chunk-{i:03d}.wav"
            with wave.open(str(chunk_path), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(rate)
                out.writeframes(raw[bounds[i] * 2 : bounds[i + 1] * 2])
            chunks.append(chunk_path)
        return chunks
    except Exception as exc:
        print(f"whisper: chunk split failed ({exc})", file=sys.stderr, flush=True)
        return []


class WhisperCli:
    """Transcribe via the whisper-cli binary (one process per recording).

    Why not the long-running server: whisper-server produced non-deterministic,
    frequently-truncated output on long audio (same file -> 948..1289 chars),
    while whisper-cli is deterministic and complete (1246 every time). The only
    cost is ~2s model-load per call (the 1.5GB model stays in the OS page cache).
    Exposes the same start()/stop()/transcribe() interface as WhisperServer."""

    def __init__(
        self,
        binary: Path,
        model_path: Path,
        language: str = "auto",
        initial_prompt: str = "",
        beam_size: int = 5,
        best_of: int = 5,
        timeout: float = 600.0,
        suppress_nst: bool = True,
        vad: bool = True,
        vad_model: Path | None = None,
    ) -> None:
        self.binary = binary
        self.model_path = model_path
        self.language = language
        self.initial_prompt = initial_prompt.strip()
        self.beam_size = beam_size
        self.best_of = best_of
        self.timeout = timeout
        self.suppress_nst = suppress_nst
        self.vad = vad
        self.vad_model = vad_model

    def start(self) -> None:
        if not self.binary.exists():
            raise FileNotFoundError(f"whisper-cli binary not found: {self.binary}")
        if not self.model_path.exists():
            raise FileNotFoundError(f"Whisper model not found: {self.model_path}")

    def stop(self) -> None:
        pass

    def transcribe(self, audio_path: Path) -> str:
        return self.transcribe_detailed(audio_path).text

    def transcribe_detailed(
        self,
        audio_path: Path,
        *,
        beam_size: int | None = None,
        best_of: int | None = None,
        use_vad: bool | None = None,
    ) -> WhisperResponse:
        audio_path = Path(audio_path)
        selected_beam = self.beam_size if beam_size is None else beam_size
        selected_best_of = self.best_of if best_of is None else best_of
        selected_vad = self.vad if use_vad is None else use_vad
        with tempfile.TemporaryDirectory(prefix="whisper-dictate-") as tmp_dir:
            output_base = Path(tmp_dir) / "result"
            args = self._build_args(
                audio_path,
                beam_size=selected_beam,
                best_of=selected_best_of,
                use_vad=selected_vad,
            )
            args += ["-ojf", "-of", str(output_base)]
            try:
                proc = subprocess.run(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise WhisperServerError(f"whisper-cli timed out after {self.timeout:.0f}s") from exc
            if proc.returncode != 0:
                details = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
                tail = " | ".join(details[-3:])
                suffix = f": {tail}" if tail else ""
                raise WhisperServerError(f"whisper-cli failed (exit {proc.returncode}){suffix}")

            json_path = output_base.with_suffix(".json")
            try:
                # whisper-cli emits byte-level tokens, so a single Cyrillic letter
                # can be split across tokens and leave a lone lead byte (0xd0) in
                # the JSON -- invalid UTF-8. Strict decoding raised UnicodeDecodeError
                # (a ValueError, NOT caught below), which crashed the whole app
                # mid-transcription. Decode tolerantly: the replacement char only
                # ever lands in a per-token `text` field (used for probabilities,
                # not output); the segment-level `text` we actually return is intact.
                payload = json.loads(json_path.read_bytes().decode("utf-8", errors="replace"))
            except (OSError, ValueError):
                text = proc.stdout.decode("utf-8", errors="replace")
                text = " ".join(line.strip() for line in text.splitlines() if line.strip()).strip()
                return WhisperResponse(
                    text=text,
                    backend="cli",
                    duration=_wav_duration(audio_path),
                )

            segments = payload.get("transcription") or []
            text = " ".join(str(segment.get("text") or "").strip() for segment in segments).strip()
            probabilities = []
            for segment in segments:
                for token in segment.get("tokens") or []:
                    token_text = str(token.get("text") or "")
                    if token_text.startswith("[_"):
                        continue
                    probability = token.get("p")
                    if probability is not None:
                        probabilities.append(probability)
            result = payload.get("result") or {}
            return WhisperResponse(
                text=text,
                backend="cli-vad" if selected_vad else "cli",
                duration=_wav_duration(audio_path),
                language=result.get("language"),
                avg_token_probability=_mean(probabilities),
            )

    def _build_args(
        self,
        audio_path: Path,
        *,
        beam_size: int,
        best_of: int,
        use_vad: bool,
    ) -> list[str]:
        args = [
            str(self.binary),
            "-m", str(self.model_path),
            "-f", str(audio_path),
            "-l", self.language,
            "-nt",
            "-bs", str(beam_size),
            "-bo", str(best_of),
        ]
        if self.suppress_nst:
            # Suppress non-speech tokens: cuts subtitle-style hallucinations
            # ("Субтитры делал ...", "Продолжение следует") on quiet/paused audio.
            args += ["-sns"]
        if use_vad and self.vad_model is not None and self.vad_model.exists():
            # Voice Activity Detection: feed only speech to the decoder. Stops
            # hallucination on silent pauses AND recovers content the plain
            # decoder dropped on long recordings.
            args += ["--vad", "-vm", str(self.vad_model)]
        if self.initial_prompt:
            args += ["--prompt", self.initial_prompt]
        return args


class WhisperServer:
    """Spawns whisper-server as a child process and exposes a simple transcribe() method."""

    def __init__(
        self,
        binary: Path,
        model_path: Path,
        language: str = "auto",
        initial_prompt: str = "",
        startup_timeout: float = 30.0,
        beam_size: int = 5,
        best_of: int = 5,
        suppress_nst: bool = True,
        vad_model: Path | None = None,
        no_fallback: bool = True,
    ) -> None:
        self.binary = binary
        self.model_path = model_path
        self.language = language
        self.initial_prompt = initial_prompt.strip()
        self.startup_timeout = startup_timeout
        self.beam_size = beam_size
        self.best_of = best_of
        self.suppress_nst = suppress_nst
        self.vad_model = vad_model
        self.no_fallback = no_fallback
        self._process: subprocess.Popen | None = None
        self._port: int | None = None

    @property
    def url(self) -> str:
        if self._port is None:
            raise WhisperServerError("Server is not running")
        return f"http://127.0.0.1:{self._port}/inference"

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
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
            # The server defaults to greedy decoding (beam-size -1), which drops
            # noticeably more words on long audio than whisper-cli's beam search.
            "-bs", str(self.beam_size),
            "-bo", str(self.best_of),
        ]
        if self.no_fallback:
            args.append("-nf")
        if self.suppress_nst:
            args.append("-sns")
        # The model path can be configured at startup while VAD itself is toggled
        # per request, which lets the A/B path use one loaded Whisper context.
        if self.vad_model is not None and self.vad_model.exists():
            args += ["-vm", str(self.vad_model)]
        args.append("-nlp")
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
            self._process.wait(timeout=3)
        finally:
            self._process = None
            self._port = None

    def transcribe(self, audio_path: Path) -> str:
        return self.transcribe_detailed(audio_path).text

    def transcribe_detailed(
        self,
        audio_path: Path,
        *,
        beam_size: int | None = None,
        best_of: int | None = None,
        use_vad: bool = False,
    ) -> WhisperResponse:
        if self._port is None:
            raise WhisperServerError("Server is not running")

        fields = {
            "response_format": "verbose_json",
            "language": self.language,
            "beam_size": str(self.beam_size if beam_size is None else beam_size),
            "best_of": str(self.best_of if best_of is None else best_of),
            "temperature": "0.0",
            "temperature_inc": "0.0",
            "suppress_nst": "true" if self.suppress_nst else "false",
            "vad": "true" if use_vad else "false",
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
        # Таймаут от длины аудио, а не слепые 10 минут: тёплый сервер делает
        # 75с куска за ~5-8с, так что 45с + двойная длительность — щедро. Раньше
        # зависший запрос держал движок (и всё приложение в 'processing') до 600с.
        duration_hint = _wav_duration(Path(audio_path)) or 60.0
        request_timeout = min(240.0, 45.0 + 2.0 * duration_hint)
        try:
            with urllib.request.urlopen(req, timeout=request_timeout) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise WhisperServerError(f"whisper-server request failed: {exc}") from exc
        except TimeoutError as exc:
            raise WhisperServerError(
                f"whisper-server request timed out after {request_timeout:.0f}s"
            ) from exc

        try:
            # strict=False: tolerate raw newlines/control chars the server may
            # emit inside the text field (more common with beam-search output).
            data = json.loads(payload, strict=False)
        except json.JSONDecodeError as exc:
            raise WhisperServerError(f"whisper-server returned invalid JSON: {payload[:200]}") from exc

        segments = data.get("segments") or []
        token_probabilities = []
        for segment in segments:
            for word in segment.get("words") or []:
                probability = word.get("probability")
                if probability is not None:
                    token_probabilities.append(probability)
        return WhisperResponse(
            text=(data.get("text") or "").strip(),
            backend="server-vad" if use_vad else "server",
            duration=data.get("duration") or _wav_duration(Path(audio_path)),
            language=data.get("language"),
            avg_token_probability=_mean(token_probabilities),
            avg_logprob=_mean(segment.get("avg_logprob") for segment in segments),
            no_speech_probability=_mean(segment.get("no_speech_prob") for segment in segments),
        )

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


class WhisperEngine:
    """Serialize inference and keep one model-loaded server for short dictations.

    Longer recordings use the deterministic CLI path. The persistent server is
    stopped around those calls so the machine never holds two ~2 GB model copies
    at once. Any server failure degrades to the CLI instead of breaking dictation.
    """

    def __init__(
        self,
        cli: WhisperCli,
        server: WhisperServer | None = None,
        *,
        persistent_max_seconds: float = 30.0,
        use_vad: bool = False,
        vad_min_duration_seconds: float = 25.0,
        confidence_retry_threshold: float = 0.80,
        min_chars_per_second: float = 0.5,
        chunk_min_seconds: float = 40.0,
        chunk_max_seconds: float = 75.0,
    ) -> None:
        self.cli = cli
        self.server = server
        self.persistent_max_seconds = persistent_max_seconds
        self.use_vad = use_vad
        self.vad_min_duration_seconds = vad_min_duration_seconds
        self.confidence_retry_threshold = confidence_retry_threshold
        self.min_chars_per_second = min_chars_per_second
        self.chunk_min_seconds = chunk_min_seconds
        self.chunk_max_seconds = chunk_max_seconds
        self._lock = threading.Lock()
        self._server_available = False

    def start(self) -> None:
        with self._lock:
            self.cli.start()
            if self.server is None or self.persistent_max_seconds <= 0:
                return
            try:
                self.server.start()
                self._server_available = True
            except Exception as exc:
                self._server_available = False
                print(f"whisper: persistent server unavailable ({exc}); using CLI", file=sys.stderr, flush=True)

    def stop(self) -> None:
        with self._lock:
            if self.server is not None:
                self.server.stop()
            self.cli.stop()
            self._server_available = False

    def transcribe(self, audio_path: Path) -> str:
        return self.transcribe_detailed(audio_path).text

    def transcribe_detailed(self, audio_path: Path) -> WhisperResponse:
        audio_path = Path(audio_path)
        duration = _wav_duration(audio_path)
        audio_peak = _wav_peak(audio_path)
        with self._lock:
            request_vad = bool(
                self.use_vad
                and duration is not None
                and duration >= self.vad_min_duration_seconds
            )
            if (
                self.server is not None
                and self._server_available
                and duration is not None
                and duration <= self.persistent_max_seconds
            ):
                try:
                    response = self.server.transcribe_detailed(audio_path, use_vad=request_vad)
                    response = replace(response, audio_peak=audio_peak)
                    if self._should_retry(response):
                        self.server.stop()
                        self._server_available = False
                        retry = self._run_cli_then_restart(audio_path, request_vad)
                        return replace(retry, audio_peak=audio_peak, retried=True)
                    return response
                except Exception as exc:
                    print(f"whisper: persistent request failed ({exc}); falling back to CLI", file=sys.stderr, flush=True)
                    self.server.stop()
                    self._server_available = False
                    response = self._run_cli_then_restart(audio_path, request_vad)
                    return replace(response, audio_peak=audio_peak, retried=True)

            if self._server_available and self.server is not None:
                # Long recording with a live server: cut it at pauses and feed
                # the pieces through the already-loaded model. A/B on an 8.6-min
                # file: single-pass VAD kept 68% of the reference volume, chunks
                # kept 99.6% -- and each chunk starts with fresh decoder context,
                # which is what prevents the long-audio repetition loops.
                try:
                    response = self._transcribe_chunked(audio_path)
                    if response is not None and not self._should_retry(response):
                        return replace(response, audio_peak=audio_peak)
                    if response is not None:
                        print(
                            "whisper: chunked result looked bad; retrying via CLI",
                            file=sys.stderr, flush=True,
                        )
                except Exception as exc:
                    print(f"whisper: chunked transcription failed ({exc}); falling back to CLI", file=sys.stderr, flush=True)
                self.server.stop()
                self._server_available = False
                response = self._run_cli_then_restart(audio_path, request_vad)
                return replace(response, audio_peak=audio_peak, retried=True)
            response = self.cli.transcribe_detailed(audio_path, use_vad=request_vad)
            return replace(response, audio_peak=audio_peak)

    def transcribe_live_chunk(self, chunk_path: Path) -> str:
        """Расшифровать один кусок диктовки, пока запись ещё идёт.

        Единственный источник — тёплый сервер: холодный CLI здесь слишком
        дорог. Если сервера нет — исключение, и живой чанкер отдаёт работу
        обычному пути по полному файлу."""
        with self._lock:
            if self.server is None or self.persistent_max_seconds <= 0:
                raise WhisperServerError("no persistent server for live chunks")
            if not self._server_available:
                self.server.start()
                self._server_available = True
            response = self.server.transcribe_detailed(chunk_path, use_vad=False)
            return response.text

    def _transcribe_chunked(self, audio_path: Path) -> WhisperResponse | None:
        """Transcribe a long recording as pause-aligned chunks via the warm server."""
        with tempfile.TemporaryDirectory(prefix="whisper-chunks-") as tmp:
            chunks = _split_wav_on_silence(
                audio_path,
                Path(tmp),
                min_seconds=self.chunk_min_seconds,
                max_seconds=self.chunk_max_seconds,
            )
            if len(chunks) < 2:
                return None  # could not split -> caller uses the single-pass path
            texts: list[str] = []
            probs: list[float | None] = []
            logps: list[float | None] = []
            nsps: list[float | None] = []
            language: str | None = None
            for chunk in chunks:
                resp = self.server.transcribe_detailed(chunk, use_vad=False)
                texts.append(resp.text)
                probs.append(resp.avg_token_probability)
                logps.append(resp.avg_logprob)
                nsps.append(resp.no_speech_probability)
                language = language or resp.language
            return WhisperResponse(
                text=" ".join(t for t in texts if t).strip(),
                backend="server-chunked",
                duration=_wav_duration(audio_path),
                language=language,
                avg_token_probability=_mean(probs),
                avg_logprob=_mean(logps),
                no_speech_probability=_mean(nsps),
            )

    def _should_retry(self, response: WhisperResponse) -> bool:
        if (
            response.avg_token_probability is not None
            and response.avg_token_probability < self.confidence_retry_threshold
        ):
            return True
        if (
            response.duration is not None
            and response.duration >= 5
            and (response.audio_peak or 0) >= 0.006
            and len(response.text) / response.duration < self.min_chars_per_second
        ):
            return True
        return False

    def warmup(self, warmup_wav: Path | None = None) -> None:
        """Touch the loaded model so its weights are resident before the real request.

        Metal drops the model's GPU residency after ~3 min idle and macOS swaps
        the idle server's memory after hours, so the first dictation after a
        break paid 1-3 extra seconds to page 1.6 GB back in. Called in the
        background when recording STARTS, so the paging overlaps with the user
        speaking. Also brings a dead server back up (self-healing)."""
        if not self._lock.acquire(blocking=False):
            return  # engine is busy transcribing => already hot
        try:
            if self.server is None or self.persistent_max_seconds <= 0:
                return
            if not self._server_available:
                try:
                    self.server.start()
                    self._server_available = True
                except Exception as exc:
                    print(f"whisper: warmup start failed ({exc})", file=sys.stderr, flush=True)
                    return
            if warmup_wav is None or not warmup_wav.exists():
                return
            try:
                self.server.transcribe_detailed(warmup_wav, use_vad=False)
            except Exception as exc:
                print(f"whisper: warmup ping failed ({exc})", file=sys.stderr, flush=True)
                self.server.stop()
                self._server_available = False
        finally:
            self._lock.release()

    def _run_cli_then_restart(self, audio_path: Path, use_vad: bool) -> WhisperResponse:
        try:
            return self.cli.transcribe_detailed(audio_path, use_vad=use_vad)
        finally:
            # Restart in the background: reloading the model takes ~3s and used
            # to be charged to the dictation that had just finished -- the text
            # only reached the clipboard after the server was back up. The
            # thread serializes on the engine lock, so it runs right after the
            # current transcription returns.
            threading.Thread(target=self._locked_restart_server, daemon=True).start()

    def _locked_restart_server(self) -> None:
        with self._lock:
            if self._server_available:
                return  # a warmup already brought it back
            self._restart_server()

    def _restart_server(self) -> None:
        if self.server is None or self.persistent_max_seconds <= 0:
            return
        try:
            self.server.start()
            self._server_available = True
        except Exception as exc:
            print(f"whisper: persistent server restart failed ({exc})", file=sys.stderr, flush=True)
            self._server_available = False


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
