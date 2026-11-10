from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path

from whisper_server import WhisperCli, WhisperEngine, WhisperResponse


def make_wav(path: Path, duration: float) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * int(16000 * duration))


class FakeCli:
    def __init__(self) -> None:
        self.calls = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def transcribe_detailed(self, _path, *, use_vad=False, **_kwargs):
        self.calls.append(use_vad)
        return WhisperResponse("cli", "cli-vad" if use_vad else "cli")


class FakeServer:
    def __init__(self, fail: bool = False, probability: float | None = None) -> None:
        self.fail = fail
        self.probability = probability
        self.started = 0
        self.stopped = 0
        self.calls = []

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def transcribe_detailed(self, _path, *, use_vad=False, **_kwargs):
        self.calls.append(use_vad)
        if self.fail:
            self.fail = False
            raise RuntimeError("server failed")
        return WhisperResponse(
            "server",
            "server-vad" if use_vad else "server",
            avg_token_probability=self.probability,
        )


class EngineTests(unittest.TestCase):
    def test_cli_start_only_validates_required_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "whisper-cli"
            model = Path(tmp) / "model.bin"
            binary.touch()
            model.touch()
            WhisperCli(binary, model).start()

    def test_short_request_uses_persistent_server_without_vad(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.wav"
            make_wav(path, 10)
            cli, server = FakeCli(), FakeServer()
            engine = WhisperEngine(cli, server, use_vad=True, vad_min_duration_seconds=25)
            engine.start()
            result = engine.transcribe_detailed(path)
            self.assertEqual(result.backend, "server")
            self.assertEqual(server.calls, [False])

    def test_vad_starts_at_configured_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "medium.wav"
            make_wav(path, 28)
            cli, server = FakeCli(), FakeServer()
            engine = WhisperEngine(cli, server, use_vad=True, vad_min_duration_seconds=25)
            engine.start()
            result = engine.transcribe_detailed(path)
            self.assertEqual(result.backend, "server-vad")
            self.assertEqual(server.calls, [True])

    def test_server_failure_falls_back_and_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.wav"
            make_wav(path, 5)
            cli, server = FakeCli(), FakeServer(fail=True)
            engine = WhisperEngine(cli, server)
            engine.start()
            result = engine.transcribe_detailed(path)
            self.assertEqual(result.backend, "cli")
            self.assertTrue(result.retried)
            self.assertEqual(server.started, 2)

    def test_low_confidence_server_result_is_retried_with_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.wav"
            make_wav(path, 5)
            cli, server = FakeCli(), FakeServer(probability=0.5)
            engine = WhisperEngine(cli, server, confidence_retry_threshold=0.8)
            engine.start()
            result = engine.transcribe_detailed(path)
            self.assertEqual(result.backend, "cli")
            self.assertTrue(result.retried)
            self.assertEqual(cli.calls, [False])


if __name__ == "__main__":
    unittest.main()
