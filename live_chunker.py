from __future__ import annotations

import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from recorder import _normalize


class LiveChunker:
    """Транскрибирует длинную диктовку ПОКА она записывается.

    Каждые ~40-75 секунд записи отрезается кусок по самой тихой точке (пауза
    дыхания) и сразу уходит на тёплый whisper-server. К моменту отпускания
    кнопки почти всё уже расшифровано — остаётся только хвост (~секунды даже
    для 10-минутной диктовки). Это архитектура Wispr Flow, но локально.

    Любой сбой делает чанкер невалидным, и приложение просто расшифровывает
    сохранённый полный WAV обычным путём — хуже, чем было, стать не может."""

    def __init__(
        self,
        recorder,
        engine,
        min_seconds: float = 40.0,
        max_seconds: float = 75.0,
    ) -> None:
        self.recorder = recorder
        self.engine = engine
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self.texts: list[str] = []
        self.valid = True
        self._buf: list[np.ndarray] = []
        self._buffered = 0          # сэмплов в _buf (ещё не отрезано)
        self._consumed_frames = 0   # сколько кадров recorder._frames уже забрано
        self._cut_samples = 0       # сэмплов, ушедших в готовые чанки (от начала записи)
        self._stop = threading.Event()
        self._done = threading.Event()
        self._tmp = Path(tempfile.mkdtemp(prefix="whisper-live-"))
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def finish(self, timeout: float = 45.0) -> tuple[list[str], int] | None:
        """Останавливает нарезку, дожидается текущей расшифровки.

        Возвращает (тексты_чанков, сэмплов_уже_расшифровано) или None, если
        живых чанков нет / что-то пошло не так — тогда вызывающий код идёт
        обычным путём по полному файлу."""
        self._stop.set()
        self._done.wait(timeout)
        if self.valid and self._done.is_set() and self.texts:
            return list(self.texts), self._cut_samples
        return None

    def cancel(self) -> None:
        self._stop.set()

    # --- внутреннее -----------------------------------------------------------

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._pull()
                if not self._maybe_cut():
                    time.sleep(0.5)
        except Exception as exc:
            print(f"live-chunker: died ({exc})", file=sys.stderr, flush=True)
            self.valid = False
        finally:
            self._done.set()

    def _pull(self) -> None:
        """Забрать новые кадры у рекордера (сами массивы не копируем — они
        уже скопированы в callback и дальше только читаются)."""
        with self.recorder._lock:
            fresh = self.recorder._frames[self._consumed_frames:]
            self._consumed_frames += len(fresh)
        for frame in fresh:
            self._buf.append(frame)
            self._buffered += len(frame)

    def _maybe_cut(self) -> bool:
        rate = int(getattr(self.recorder, "_active_sample_rate", 16000))
        if self._buffered < rate * self.max_seconds:
            return False
        audio = np.concatenate(self._buf, axis=0).reshape(-1)
        win = max(1, int(rate * 0.05))
        n_win = len(audio) // win
        lo = int(self.min_seconds * rate / win)
        hi = min(int(self.max_seconds * rate / win), n_win - 1)
        if hi <= lo:
            return False
        peaks = np.abs(audio[: n_win * win]).reshape(n_win, win).max(axis=1)
        cut = (lo + int(np.argmin(peaks[lo:hi]))) * win + win // 2

        chunk, rest = audio[:cut], audio[cut:]
        self._buf = [rest.reshape(-1, 1)]
        self._buffered = len(rest)
        self._cut_samples += cut

        path = self._tmp / f"live-{len(self.texts):03d}.wav"
        pcm = np.clip(_normalize(chunk), -1.0, 1.0)
        pcm = (pcm * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(pcm.tobytes())

        text = self.engine.transcribe_live_chunk(path)
        self.texts.append(text)
        print(
            f"live-chunker: chunk {len(self.texts)} done ({cut / rate:.0f}s audio)",
            file=sys.stderr, flush=True,
        )
        return True
