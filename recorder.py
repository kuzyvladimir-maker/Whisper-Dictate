from __future__ import annotations

import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd


def _reset_portaudio() -> None:
    """Re-scan audio devices. PortAudio caches the device list at import; it goes
    stale after the default input device changes or the Mac sleeps/wakes, which
    makes opening an InputStream fail with PaErrorCode -9986 (paInternalError)."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass


def _default_input_samplerate() -> int:
    try:
        return int(sd.query_devices(kind="input")["default_samplerate"])
    except Exception:
        return 48000


# Below this 99.5th-percentile level the recording is almost certainly silence /
# room noise, not speech. We must NOT amplify it: blowing noise up to speech
# level is exactly what makes Whisper hallucinate subtitle credits.
SILENCE_LEVEL = 0.006


def _normalize(audio: np.ndarray, target: float = 0.95, max_gain: float = 18.0) -> np.ndarray:
    """Bring quiet recordings up to a usable level.

    Mic input is often very quiet (low system gain / distance from mic). Whisper
    then treats whole stretches as silence and drops them, which truncated long
    dictations. We scale so the 99.5th-percentile amplitude reaches `target`
    (robust to rare loud transients, which are simply clipped), never reducing
    already-loud audio and capping the boost so near-silence isn't blown up."""
    if audio.size == 0:
        return audio
    level = float(np.percentile(np.abs(audio), 99.5))
    if level < SILENCE_LEVEL:  # silence / noise -- leave it quiet, don't feed Whisper amplified noise
        print(f"recorder: near-silent input (level={level:.4f}) -- not amplifying", file=sys.stderr, flush=True)
        return audio
    gain = min(target / level, max_gain)
    if gain <= 1.0:
        return audio
    return np.clip(audio * gain, -1.0, 1.0)


class AudioRecorder:
    def __init__(self, sample_rate: int = 16000, channels: int = 1) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self._active_sample_rate = sample_rate
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None
        # Smoothed live input peak (0..1), read by the level monitor while
        # recording to warn on-screen when the mic isn't picking anything up.
        self.current_level = 0.0
        # Ставится, когда зависший поток пришлось бросить: перед следующим
        # стартом PortAudio нужно пересоздать.
        self._needs_reset = False
        self.max_level = 0.0
        self.last_callback_at = 0.0
        # Микрофон держится ОТКРЫТЫМ между диктовками (см. start/stop): на
        # каждое открытие/закрытие CoreAudio даёт ~2.5% шанс вечного зависания,
        # а при плотной работе это десятки зависаний в день. Поток закрывается
        # лениво, только после паузы, фоновым таймером.
        self._capturing = False
        self._stream_device = ""
        # Сколько зависших потоков мы бросили. Пока он не ноль, трогать
        # Pa_Terminate смертельно опасно (см. _open_stream).
        self._abandoned = 0
        self._idle_close_timer: threading.Timer | None = None
        self.idle_close_seconds = 90.0

    @property
    def is_recording(self) -> bool:
        return self._capturing

    def start(self) -> None:
        if self._capturing:
            return

        self._cancel_idle_close()
        with self._lock:
            self._frames = []
        self.current_level = 0.0
        self.max_level = 0.0
        self.last_callback_at = time.monotonic()

        self._ensure_stream()
        self._capturing = True

    def _ensure_stream(self) -> None:
        """Открыть микрофон, если он ещё не открыт (или устарел).

        Переиспользование уже открытого потока — главная защита от зависаний:
        в горячем пути диктовки вообще не остаётся вызовов CoreAudio."""
        device = self.input_device_name()
        if self._stream is not None:
            stale = device != self._stream_device
            try:
                stale = stale or not self._stream.active
            except Exception:
                stale = True
            if stale:
                print(
                    f"recorder: input changed ({self._stream_device!r} -> {device!r}); reopening",
                    file=sys.stderr, flush=True,
                )
                self._close_stream_in_background(self._stream)
                self._stream = None
        if self._stream is not None:
            return

        if self._needs_reset:
            # НЕЛЬЗЯ вызывать _reset_portaudio() (sd._terminate): Pa_Terminate
            # закрывает все открытые потоки, включая брошенный зависший, и
            # входит ровно в тот же дедлок. Именно это превращало зависание
            # остановки в зависание старта (97 раз в логе). Брошенный поток
            # просто оставляем гнить — новый открывается независимо от него.
            print("recorder: previous stream was abandoned; opening a fresh one", file=sys.stderr, flush=True)
            self._needs_reset = False

        stream = self._open_stream()
        try:
            stream.start()
        except Exception:
            # Do not leave a half-open stream behind. A stale non-None value made
            # every later start() return early, so dictation appeared frozen.
            try:
                stream.close()
            except Exception:
                pass
            raise
        self._stream = stream
        self._stream_device = device

    # --- ленивое закрытие микрофона ------------------------------------------

    def _cancel_idle_close(self) -> None:
        timer = self._idle_close_timer
        self._idle_close_timer = None
        if timer is not None:
            timer.cancel()

    def _arm_idle_close(self) -> None:
        """Закрыть микрофон после паузы: гаснет индикатор записи в строке меню,
        а само закрытие происходит в фоне, где зависание никому не мешает."""
        self._cancel_idle_close()
        if self.idle_close_seconds <= 0:
            # Закрыть сразу — индикатор микрофона гаснет почти мгновенно.
            # Закрытие идёт в фоне, так что даже зависание не задержит вставку.
            self._close_idle_stream()
            return
        timer = threading.Timer(self.idle_close_seconds, self._close_idle_stream)
        timer.daemon = True
        self._idle_close_timer = timer
        timer.start()

    def _close_idle_stream(self) -> None:
        if self._capturing or self._stream is None:
            return
        stream = self._stream
        self._stream = None
        self._stream_device = ""
        self._close_stream_in_background(stream)

    def _close_stream_in_background(self, stream) -> None:
        threading.Thread(
            target=self._close_stream_with_timeout, args=(stream,), daemon=True
        ).start()

    def _open_stream(self) -> sd.InputStream:
        """Open the mic, recovering from a stale/changed audio device.

        Retries after re-scanning devices (fixes PaErrorCode -9986), and finally
        falls back to the device's native sample rate if it rejects 16 kHz."""
        # 16 kHz first (Whisper-native); on failure fall back to the device's own
        # rate, which is what flaky Bluetooth mics (AirPods switch to 24 kHz when
        # the mic turns on) actually want.
        attempts = [self.sample_rate, None, None]  # None -> device default rate
        last_err: Exception | None = None
        for i, rate in enumerate(attempts):
            try:
                sr = int(rate) if rate else _default_input_samplerate()
                stream = sd.InputStream(
                    samplerate=sr,
                    channels=self.channels,
                    dtype="float32",
                    # ЯВНЫЙ blocksize обязателен на CoreAudio. С blocksize=0
                    # PortAudio включает свой конвертер и вешает на поток
                    # property-listener (startStopCallback), который при
                    # остановке лезет в AudioUnitGetProperty из аудио-потока и
                    # схлопывается в ABBA-дедлок с AudioOutputUnitStop.
                    # Замерено на этой машине: без blocksize зависание на
                    # 4-99 цикле (4 прогона из 4), с blocksize=1024 — 520
                    # циклов без единого зависания.
                    blocksize=1024,
                    callback=self._callback,
                )
                self._active_sample_rate = sr
                if i > 0:
                    print(f"recorder: opened mic on attempt {i + 1} at {sr} Hz", file=sys.stderr, flush=True)
                return stream
            except Exception as exc:
                last_err = exc
                if self._abandoned:
                    # Pa_Terminate закрыл бы брошенный зависший поток и встал
                    # бы в тот же дедлок. Лучше просто попробовать ещё раз.
                    print(f"recorder: mic open failed ({exc}); retrying without PortAudio reset", file=sys.stderr, flush=True)
                else:
                    print(f"recorder: mic open failed ({exc}); rescanning devices", file=sys.stderr, flush=True)
                    _reset_portaudio()
                time.sleep(0.25)
        raise last_err if last_err else RuntimeError("could not open microphone")

    def input_device_name(self) -> str:
        """Name of the current default input device ("" if unknown). Used to
        decide how long to delay the start cue: only Bluetooth mics (AirPods)
        swallow a sound played right as the mic opens."""
        try:
            return str(sd.query_devices(kind="input")["name"])
        except Exception:
            return ""

    def captured_peak(self) -> float:
        """Loudest sample captured so far (0..1). Used to decide whether a very
        short recording actually contains speech before discarding it."""
        with self._lock:
            frames = list(self._frames)
        if not frames:
            return 0.0
        try:
            return max(float(np.abs(f).max()) for f in frames)
        except (ValueError, TypeError):
            return 0.0

    def abort(self) -> None:
        """Stop recording and throw away the audio (for discarded stray taps)."""
        if not self._capturing:
            return
        self._capturing = False
        with self._lock:
            self._frames = []
        self._arm_idle_close()

    def _close_stream_with_timeout(self, stream, timeout: float = 3.0) -> None:
        """stop()/close() потока — в отдельном потоке с лимитом времени.

        CoreAudio/PortAudio при остановке иногда виснет НАМЕРТВО (не ошибка, а
        вечная блокировка) — из-за этого приложение застревало в 'processing' и
        переставало реагировать на кнопки. Захваченные кадры уже лежат в
        _frames, так что зависший поток можно просто бросить и жить дальше."""
        def _close() -> None:
            try:
                stream.stop()
            except Exception as exc:
                print(f"recorder: stream stop failed ({exc}); saving captured audio", file=sys.stderr, flush=True)
            try:
                stream.close()
            except Exception as exc:
                print(f"recorder: stream close failed ({exc})", file=sys.stderr, flush=True)

        closer = threading.Thread(target=_close, daemon=True)
        closer.start()
        closer.join(timeout)
        if closer.is_alive():
            print(
                f"recorder: stream stop hung > {timeout:.0f}s -- abandoning it, captured audio is safe",
                file=sys.stderr, flush=True,
            )
            self._needs_reset = True
            self._abandoned += 1

    def stop(self, output_path: Path) -> Path:
        if not self._capturing:
            raise RuntimeError("Recorder is not running")

        # Микрофон НЕ закрываем: поток переиспользуется следующей диктовкой, а
        # закроется сам после паузы. Именно закрытие/открытие на каждой записи
        # давало зависания CoreAudio (2.5% диктовок).
        self._capturing = False
        self._arm_idle_close()

        with self._lock:
            frames = list(self._frames)
            self._frames = []
        if not frames:
            raise RuntimeError("No audio was recorded")
        audio = np.concatenate(frames, axis=0)

        audio = _normalize(audio)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        pcm16 = np.clip(audio, -1.0, 1.0)
        pcm16 = (pcm16 * 32767).astype(np.int16)
        # Whisper tends to drop the last word when speech runs right into the
        # end of the file (the key is released mid-breath). A short silent tail
        # gives the decoder room to finish the sentence.
        tail = np.zeros((int(self._active_sample_rate * 0.7),) + pcm16.shape[1:], dtype=np.int16)
        pcm16 = np.concatenate([pcm16, tail])

        with wave.open(str(output_path), "wb") as wav:
            wav.setnchannels(self.channels)
            wav.setsampwidth(2)
            wav.setframerate(self._active_sample_rate)
            wav.writeframes(pcm16.tobytes())

        return output_path

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            # sounddevice reports xruns here. Keep recording unless the stream fails.
            pass
        if self._capturing:
            with self._lock:
                self._frames.append(indata.copy())
        # Smoothed peak: rises instantly on sound, decays slowly so short pauses
        # between words don't read as silence.
        try:
            peak = float(np.abs(indata).max())
        except (ValueError, TypeError):
            peak = 0.0
        self.current_level = max(peak, self.current_level * 0.9)
        self.max_level = max(self.max_level, peak)
        self.last_callback_at = time.monotonic()
