from __future__ import annotations

import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd


class AudioRecorder:
    def __init__(self, sample_rate: int = 16000, channels: int = 1) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return

        with self._lock:
            self._frames = []

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self, output_path: Path) -> Path:
        if self._stream is None:
            raise RuntimeError("Recorder is not running")

        stream = self._stream
        self._stream = None
        stream.stop()
        stream.close()

        with self._lock:
            if not self._frames:
                raise RuntimeError("No audio was recorded")
            audio = np.concatenate(self._frames, axis=0)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        pcm16 = np.clip(audio, -1.0, 1.0)
        pcm16 = (pcm16 * 32767).astype(np.int16)

        with wave.open(str(output_path), "wb") as wav:
            wav.setnchannels(self.channels)
            wav.setsampwidth(2)
            wav.setframerate(self.sample_rate)
            wav.writeframes(pcm16.tobytes())

        return output_path

    def _callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        if status:
            # sounddevice reports xruns here. Keep recording unless the stream fails.
            pass
        with self._lock:
            self._frames.append(indata.copy())
