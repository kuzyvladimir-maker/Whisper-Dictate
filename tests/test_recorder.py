from __future__ import annotations

import tempfile
import time
import unittest
import wave
from pathlib import Path

import numpy as np

from recorder import AudioRecorder


class FailingStartStream:
    def __init__(self) -> None:
        self.closed = False

    def start(self) -> None:
        raise RuntimeError("simulated start failure")

    def close(self) -> None:
        self.closed = True


class FailingStopStream:
    def stop(self) -> None:
        raise RuntimeError("simulated stop failure")

    def close(self) -> None:
        pass


class RecorderTests(unittest.TestCase):
    def test_callback_updates_activity_timestamp(self) -> None:
        recorder = AudioRecorder()
        before = time.monotonic()
        recorder._callback(
            np.full((160, 1), 0.2, dtype=np.float32),
            160,
            object(),
            None,
        )

        self.assertGreaterEqual(recorder.last_callback_at, before)
        self.assertEqual(len(recorder._frames), 1)

    def test_start_failure_does_not_leave_stale_stream(self) -> None:
        recorder = AudioRecorder()
        stream = FailingStartStream()
        recorder._open_stream = lambda: stream
        with self.assertRaises(RuntimeError):
            recorder.start()
        self.assertFalse(recorder.is_recording)
        self.assertTrue(stream.closed)

    def test_stop_failure_still_saves_captured_audio(self) -> None:
        recorder = AudioRecorder()
        recorder._stream = FailingStopStream()
        recorder._frames = [np.full((160, 1), 0.2, dtype=np.float32)]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "audio.wav"
            recorder.stop(output)
            self.assertTrue(output.exists())
            with wave.open(str(output), "rb") as wav:
                self.assertEqual(wav.getnframes(), 160)


if __name__ == "__main__":
    unittest.main()
