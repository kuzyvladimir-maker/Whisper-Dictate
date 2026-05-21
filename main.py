from __future__ import annotations

import atexit
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import rumps
from PyObjCTools import AppHelper

from paste import ClipboardPaste, notify
from recorder import AudioRecorder
from transcriber import Transcriber
from whisper_server import WhisperServer


def _resolve_paths() -> dict[str, Path]:
    """Return runtime paths. Two modes: dev (running from source) and frozen (installed .app)."""
    if getattr(sys, "frozen", False):
        bundle_resources = Path(sys.executable).resolve().parent.parent / "Resources"
        data_dir = Path.home() / "Library/Application Support/Whisper Dictate"
        return {
            "server": bundle_resources / "whisper-cli-bin" / "whisper-server",
            "models_dir": bundle_resources / "models",
            "recordings_dir": data_dir / "recordings",
            "data_dir": data_dir,
            "default_config": bundle_resources / "config.json",
            "user_config": data_dir / "config.json",
        }
    project_dir = Path(__file__).resolve().parent
    return {
        "server": project_dir / "vendor/whisper.cpp/build/bin/whisper-server",
        "models_dir": project_dir / "vendor/whisper.cpp/models",
        "recordings_dir": project_dir / "recordings",
        "data_dir": project_dir,
        "default_config": project_dir / "config.json",
        "user_config": project_dir / "config.json",
    }


PATHS = _resolve_paths()


class ControlServer:
    def __init__(self, socket_path: str, on_press, on_release) -> None:
        self.socket_path = Path(socket_path)
        self.on_press = on_press
        self.on_release = on_release
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _run(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        server.listen(8)
        server.settimeout(0.5)

        while not self._stop.is_set():
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            with conn:
                command = conn.recv(64).decode("utf-8", errors="ignore").strip()
                if command == "press":
                    threading.Thread(target=self.on_press, daemon=True).start()
                elif command == "release":
                    threading.Thread(target=self.on_release, daemon=True).start()
                conn.sendall(b"ok\n")

        server.close()


class WhisperDictateApp(rumps.App):
    def __init__(self) -> None:
        self.config = self._load_config()
        super().__init__("🎙", quit_button=None)

        self.state = "idle"
        self.mode = "idle"
        self.press_started_at = 0.0
        self.pending_tap_timer: threading.Timer | None = None
        self.stop_lock = threading.Lock()
        self.last_transcription = ""

        model_path = PATHS["models_dir"] / self.config["model_filename"]
        self.recorder = AudioRecorder(sample_rate=int(self.config["sample_rate"]))
        self.whisper_server = WhisperServer(
            binary=PATHS["server"],
            model_path=model_path,
            language=self.config["language"],
            initial_prompt=str(self.config.get("initial_prompt", "")),
        )
        threading.Thread(target=self._start_server_async, daemon=True).start()
        atexit.register(self.whisper_server.stop)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, lambda *_: (self.whisper_server.stop(), sys.exit(0)))
        self.transcriber = Transcriber(
            server=self.whisper_server,
            smart_cleanup=bool(self.config.get("smart_cleanup", True)),
        )
        self.clipboard = ClipboardPaste()
        self.control_server = ControlServer(
            socket_path=str(self.config.get("control_socket", "/tmp/whisper-dictate.sock")),
            on_press=self.on_hotkey_press,
            on_release=self.on_hotkey_release,
        )
        self.control_server.start()

        self.status_item = rumps.MenuItem("Ready")
        self.last_item = rumps.MenuItem("Last: -")
        self.toggle_paste_item = rumps.MenuItem("Paste after transcription", callback=self.toggle_paste)
        self.toggle_paste_item.state = bool(self.config.get("paste_after_transcription", True))
        self.menu = [
            self.status_item,
            self.last_item,
            None,
            self.toggle_paste_item,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

    def _load_config(self) -> dict:
        user = PATHS["user_config"]
        default = PATHS["default_config"]
        if user.exists() and user != default:
            return json.loads(user.read_text(encoding="utf-8"))
        return json.loads(default.read_text(encoding="utf-8"))

    def on_hotkey_press(self) -> None:
        if self.state == "processing":
            return

        now = time.monotonic()
        if self.state == "recording" and self.pending_tap_timer is not None:
            self.pending_tap_timer.cancel()
            self.pending_tap_timer = None
            self.mode = "toggle"
            self._set_status("recording", "🔴", "Recording... tap Fn/F18 again to stop")
            return

        if self.state == "recording" and self.mode == "toggle":
            self._stop_and_transcribe()
            return

        if self.state == "idle":
            self.press_started_at = now
            self.mode = "hold"
            self._start_recording()

    def on_hotkey_release(self) -> None:
        if self.state != "recording" or self.mode == "toggle":
            return

        held_for = time.monotonic() - self.press_started_at
        if held_for < float(self.config.get("tap_threshold_seconds", 0.25)):
            self._schedule_tap_stop()
            return

        self._stop_and_transcribe()

    def _start_recording(self) -> None:
        try:
            self.recorder.start()
            self._set_status("recording", "🔴", "Recording...")
        except Exception as exc:
            self._set_status("idle", "🎙", "Ready")
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            notify("Whisper Dictate", f"Recording failed: {exc}")

    def _schedule_tap_stop(self) -> None:
        if self.pending_tap_timer is not None:
            self.pending_tap_timer.cancel()
        delay = float(self.config.get("double_tap_window_seconds", 0.4))
        self.pending_tap_timer = threading.Timer(delay, self._stop_and_transcribe)
        self.pending_tap_timer.daemon = True
        self.pending_tap_timer.start()

    def _stop_and_transcribe(self) -> None:
        with self.stop_lock:
            if self.state != "recording":
                return
            if self.pending_tap_timer is not None:
                self.pending_tap_timer.cancel()
                self.pending_tap_timer = None

            self._set_status("processing", "⏳", "Transcribing...")
            audio_path = PATHS["recordings_dir"] / f"dictation-{datetime.now():%Y%m%d-%H%M%S}.wav"
            try:
                self.recorder.stop(audio_path)
                text = self.transcriber.transcribe(audio_path)
                if text:
                    self.last_transcription = text
                    self.clipboard.copy(text)
                    if bool(self.config.get("paste_after_transcription", True)):
                        self.clipboard.paste()
                    self._update_last_item(text)
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                sys.stderr.flush()
                notify("Whisper Dictate", f"Transcription failed: {exc}")
            finally:
                self.mode = "idle"
                self._set_status("idle", "🎙", "Ready")

    def _set_status(self, state: str, title: str, label: str) -> None:
        self.state = state
        AppHelper.callAfter(self._apply_status, title, label)

    def _apply_status(self, title: str, label: str) -> None:
        self.title = title
        self.status_item.title = label

    def _update_last_item(self, text: str) -> None:
        preview = text if len(text) <= 50 else text[:47] + "..."
        self.last_item.title = f"Last: {preview}"

    def toggle_paste(self, sender) -> None:
        self.config["paste_after_transcription"] = not bool(self.config.get("paste_after_transcription", True))
        sender.state = self.config["paste_after_transcription"]
        PATHS["user_config"].parent.mkdir(parents=True, exist_ok=True)
        PATHS["user_config"].write_text(
            json.dumps(self.config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _start_server_async(self) -> None:
        try:
            self.whisper_server.start()
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            notify("Whisper Dictate", f"Failed to start whisper-server: {exc}")

    def quit_app(self, _sender) -> None:
        self.control_server.stop()
        self.whisper_server.stop()
        rumps.quit_application()


if __name__ == "__main__":
    WhisperDictateApp().run()
