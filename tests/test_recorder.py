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
        # The stream stays open between dictations, so frames are kept only while
        # a dictation is being captured; the level meter updates either way.
        recorder = AudioRecorder()
        chunk = np.full((160, 1), 0.2, dtype=np.float32)
        before = time.monotonic()
        recorder._callback(chunk, 160, object(), None)
        self.assertGreaterEqual(recorder.last_callback_at, before)
        self.assertEqual(len(recorder._frames), 0)
        self.assertGreater(recorder.current_level, 0.0)

        recorder._capturing = True
        recorder._callback(chunk, 160, object(), None)
        self.assertEqual(len(recorder._frames), 1)

    def test_start_failure_does_not_leave_stale_stream(self) -> None:
        recorder = AudioRecorder()
        stream = FailingStartStream()
        recorder._open_stream = lambda: stream
        with self.assertRaises(RuntimeError):
            recorder.start()
        self.assertFalse(recorder.is_recording)
        self.assertTrue(stream.closed)

    def test_stop_saves_captured_audio_and_keeps_stream_open(self) -> None:
        # stop() no longer touches the stream (open/close per dictation hung
        # CoreAudio); a stream whose stop() raises must stay untouched.
        recorder = AudioRecorder()
        recorder.idle_close_seconds = 3600
        stream = FailingStopStream()
        recorder._stream = stream
        recorder._capturing = True
        recorder._frames = [np.full((160, 1), 0.2, dtype=np.float32)]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "audio.wav"
                recorder.stop(output)
                self.assertTrue(output.exists())
                with wave.open(str(output), "rb") as wav:
                    # captured audio + the 0.7 s silent tail added for the decoder
                    self.assertEqual(wav.getnframes(), 160 + int(16000 * 0.7))
            self.assertIs(recorder._stream, stream)
            self.assertFalse(recorder.is_recording)
        finally:
            recorder._cancel_idle_close()


if __name__ == "__main__":
    unittest.main()
