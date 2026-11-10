from __future__ import annotations

import atexit
import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import wave
from datetime import datetime
from pathlib import Path

import rumps
from PyObjCTools import AppHelper

from cleaner import MODES, TextCleaner
from live_chunker import LiveChunker
from paste import ClipboardPaste, notify, play_sound
from recorder import AudioRecorder
from transcriber import Transcriber, TranscriptionResult
from vocabulary import ICLOUD_DIR, Vocabulary
from whisper_server import (
    WhisperCli,
    WhisperEngine,
    WhisperResponse,
    WhisperServer,
    _wav_duration,
    _wav_peak,
)


# Voice recordings and transcripts are private by default. This also applies to
# files created by helper libraries and temporary atomic writes.
os.umask(0o077)


def log(message: str) -> None:
    """Timestamped line to stderr (captured by the LaunchAgent log file)."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}", file=sys.stderr, flush=True)


def _resolve_paths() -> dict[str, Path]:
    """Return runtime paths. Two modes: dev (running from source) and frozen (installed .app)."""
    if getattr(sys, "frozen", False):
        bundle_resources = Path(sys.executable).resolve().parent.parent / "Resources"
        data_dir = Path.home() / "Library/Application Support/Whisper Dictate"
        return {
            "server": bundle_resources / "whisper-cli-bin" / "whisper-server",
            "cli": bundle_resources / "whisper-cli-bin" / "whisper-cli",
            "models_dir": bundle_resources / "models",
            "vad_model": bundle_resources / "models" / "ggml-silero-v6.2.0.bin",
            "recordings_dir": data_dir / "recordings",
            "data_dir": data_dir,
            "default_config": bundle_resources / "config.json",
            "user_config": data_dir / "config.json",
        }
    project_dir = Path(__file__).resolve().parent
    return {
        "server": project_dir / "vendor/whisper.cpp/build/bin/whisper-server",
        "cli": project_dir / "vendor/whisper.cpp/build/bin/whisper-cli",
        "models_dir": project_dir / "vendor/whisper.cpp/models",
        "vad_model": project_dir / "vendor/whisper.cpp/models/ggml-silero-v6.2.0.bin",
        "recordings_dir": project_dir / "recordings",
        "data_dir": project_dir,
        "default_config": project_dir / "config.json",
        "user_config": project_dir / "config.local.json",
    }


PATHS = _resolve_paths()

BUILTIN_DEFAULTS = {
    "control_socket": "/tmp/whisper-dictate.sock",
    "language": "auto",
    "sample_rate": 16000,
    "tap_threshold_seconds": 0.4,
    "double_click_window_seconds": 0.5,
    "enter_to_stop": True,
    "paste_after_transcription": True,
    "smart_cleanup": True,
    "initial_prompt": "",
    "model_filename": "ggml-large-v3-turbo.bin",
    "sounds": True,
    "max_recording_seconds": 600,
    "keep_recordings": 200,
    "keep_transcripts": 2000,
    "history_size": 25,
    "hearing_alert": True,
    "hearing_level": 0.03,
    "hearing_min_level": 0.008,
    "hearing_alert_after": 6.0,
    "hearing_pause_alert_after": 20.0,
    "beam_size": 5,
    "best_of": 5,
    "vad": True,
    "vad_min_duration_seconds": 25,
    "vocabulary_prompt": False,
    "suppress_nst": True,
    "persistent_server": True,
    "persistent_server_max_seconds": 30,
    "confidence_retry_threshold": 0.80,
    "min_chars_per_second": 0.5,
}


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


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
        os.chmod(self.socket_path, 0o600)
        server.listen(8)
        server.settimeout(0.5)

        while not self._stop.is_set():
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                log(f"control: accept failed: {exc}")
                continue

            # A single misbehaving client (e.g. nc hangs up before reading the
            # reply -> BrokenPipeError) must NEVER kill this loop. A dead loop
            # means the app silently stops responding to the hotkey until it is
            # restarted -- the original "it froze" bug.
            try:
                self._handle_conn(conn)
            except Exception as exc:
                log(f"control: connection error: {exc}")
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

        server.close()

    def _handle_conn(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        try:
            command = conn.recv(64).decode("utf-8", errors="ignore").strip()
        except (socket.timeout, OSError):
            return

        recognized = True
        if command == "press":
            event_time = time.monotonic()
            threading.Thread(
                target=self._dispatch,
                args=(self.on_press, event_time),
                daemon=True,
            ).start()
        elif command == "release":
            event_time = time.monotonic()
            threading.Thread(
                target=self._dispatch,
                args=(self.on_release, event_time),
                daemon=True,
            ).start()
        else:
            recognized = False

        try:
            conn.sendall(b"ok\n" if recognized else b"error: unknown command\n")
        except OSError:
            # Client already gone -- harmless, the work is already dispatched.
            pass

    @staticmethod
    def _dispatch(handler, event_time: float) -> None:
        try:
            handler(event_time)
        except Exception as exc:
            log(f"control: handler {getattr(handler, '__name__', handler)} failed: {exc}")
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()


class WhisperDictateApp(rumps.App):
    def __init__(self) -> None:
        self.config = self._load_config()
        self._acquire_single_instance()
        self._kill_orphan_whisper_servers()
        super().__init__("🎙", quit_button=None)

        self.state = "idle"
        self.mode = "idle"
        self.press_started_at = 0.0
        self.max_duration_timer: threading.Timer | None = None
        self.double_click_timer: threading.Timer | None = None
        self.start_beep_timer: threading.Timer | None = None
        self.live_chunker: LiveChunker | None = None
        self.stop_lock = threading.Lock()
        self.event_lock = threading.Lock()
        self.last_transcription = ""
        self.active_context = ""
        self.last_active_context = ""
        self.sounds = bool(self.config.get("sounds", True))
        self.tap_threshold = float(self.config.get("tap_threshold_seconds", 0.4))
        self.double_click_window = float(self.config.get("double_click_window_seconds", 0.5))
        self.enter_to_stop = bool(self.config.get("enter_to_stop", True))
        self.max_recording_seconds = float(self.config.get("max_recording_seconds", 600))
        self.keep_recordings = int(self.config.get("keep_recordings", 50))
        # On-screen "we can't hear you" warning while recording.
        self.hearing_alert = bool(self.config.get("hearing_alert", True))
        self.hearing_level = float(self.config.get("hearing_level", 0.03))
        self.hearing_min_level = float(self.config.get("hearing_min_level", 0.008))
        self.hearing_alert_after = float(self.config.get("hearing_alert_after", 6.0))
        self.hearing_pause_alert_after = float(self.config.get("hearing_pause_alert_after", 20.0))
        self._hud = None
        self._hud_size = (680, 96)
        self.transcripts_path = PATHS["data_dir"] / "transcripts.txt"
        self.transcripts_jsonl_path = PATHS["data_dir"] / "transcripts.jsonl"
        self.keep_transcripts = int(self.config.get("keep_transcripts", 2000))
        self._transcripts_since_prune = 0
        self._secure_data_paths()
        self._prune_transcript_logs()

        # Shared dictionary (same file as macbook/ Lite): master in iCloud Drive when
        # available, with a local cache for an evicted or not-yet-synced file.
        dict_dir = Path(str(self.config.get("dictionary_dir", "") or "")).expanduser()
        if not str(self.config.get("dictionary_dir", "") or ""):
            dict_dir = ICLOUD_DIR if ICLOUD_DIR.parent.exists() else PATHS["data_dir"]
        self.vocabulary = Vocabulary(
            dict_dir / "dictionary.txt",
            context_dir=dict_dir / "dictionaries",
            cache_path=PATHS["data_dir"] / "dictionary.cache.txt",
            context_cache_dir=PATHS["data_dir"] / "dictionaries.cache",
        )
        log(
            f"dictionary: {len(self.vocabulary.rules)} rules, {len(self.vocabulary.terms)} terms "
            f"from {self.vocabulary._source}"
        )

        model_path = PATHS["models_dir"] / self.config["model_filename"]
        self.recorder = AudioRecorder(sample_rate=int(self.config["sample_rate"]))
        # Микрофон остаётся открытым между диктовками и закрывается сам после
        # паузы (0 = держать всегда). Это убирает открытие/закрытие CoreAudio
        # из горячего пути — источник зависаний.
        self.recorder.idle_close_seconds = float(self.config.get("mic_idle_close_seconds", 90))
        whisper_cli = WhisperCli(
            binary=PATHS["cli"],
            model_path=model_path,
            language=self.config["language"],
            # Seeding the decoder with the term list (`--prompt`) sends it into a
            # repetition loop near the end of long dictations and costs punctuation.
            # The same terms are fixed after the fact by vocabulary.apply(), so the
            # prompt is off by default; `vocabulary_prompt` re-enables it.
            initial_prompt=(
                self.vocabulary.prompt(str(self.config.get("initial_prompt", "")))
                if self.config.get("vocabulary_prompt", False)
                else str(self.config.get("initial_prompt", "")).strip()
            ),
            beam_size=int(self.config.get("beam_size", 5)),
            best_of=int(self.config.get("best_of", 5)),
            suppress_nst=bool(self.config.get("suppress_nst", True)),
            vad=bool(self.config.get("vad", True)),
            vad_model=PATHS["vad_model"],
        )
        persistent_enabled = bool(self.config.get("persistent_server", True))
        # Стилевая подсказка декодеру: короткая ЕСТЕСТВЕННАЯ фраза со знаками
        # препинания. Лечит режим «весь текст без точек и запятых», в который
        # whisper иногда сваливается с первых слов (проверено 2026-07-30: на
        # кусках <=75с объём не меняется, пунктуации +76%). Только для СЕРВЕРА:
        # он получает исключительно короткие куски. Длинному одиночному проходу
        # CLI prompt противопоказан (крадёт ~16% текста). Список терминов сюда
        # ставить НЕЛЬЗЯ (июльские петли повторов).
        style_prompt = str(self.config.get(
            "style_prompt",
            "Привет! Это моя диктовка: я говорю, а текст записывается "
            "со знаками препинания, запятыми и точками. Начинаю.",
        )).strip()
        whisper_http_server = None
        if persistent_enabled:
            whisper_http_server = WhisperServer(
                binary=PATHS["server"],
                model_path=model_path,
                language=self.config["language"],
                initial_prompt=style_prompt or whisper_cli.initial_prompt,
                beam_size=whisper_cli.beam_size,
                best_of=whisper_cli.best_of,
                suppress_nst=whisper_cli.suppress_nst,
                vad_model=PATHS["vad_model"],
            )
        self.whisper_server = WhisperEngine(
            cli=whisper_cli,
            server=whisper_http_server,
            persistent_max_seconds=float(self.config.get("persistent_server_max_seconds", 30)),
            use_vad=bool(self.config.get("vad", True)),
            vad_min_duration_seconds=float(self.config.get("vad_min_duration_seconds", 25)),
            confidence_retry_threshold=float(self.config.get("confidence_retry_threshold", 0.80)),
            min_chars_per_second=float(self.config.get("min_chars_per_second", 0.5)),
            chunk_min_seconds=float(self.config.get("chunk_min_seconds", 40)),
            chunk_max_seconds=float(self.config.get("chunk_max_seconds", 75)),
        )
        threading.Thread(target=self._start_server_async, daemon=True).start()
        atexit.register(self.whisper_server.stop)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            # Ненулевой код выхода: чужой SIGTERM/SIGHUP (чистильщики, killall,
            # логаут-глюки) -> launchd (KeepAlive.SuccessfulExit=false) поднимет
            # приложение заново. Осознанный Quit из меню выходит с кодом 0 и
            # остаётся выключенным.
            signal.signal(sig, lambda *_: (self.whisper_server.stop(), sys.exit(1)))
        self.transcriber = Transcriber(
            server=self.whisper_server,
            smart_cleanup=bool(self.config.get("smart_cleanup", True)),
            vocabulary=self.vocabulary,
        )
        self.clipboard = ClipboardPaste()
        self.control_server = ControlServer(
            socket_path=str(self.config.get("control_socket", "/tmp/whisper-dictate.sock")),
            on_press=self.on_hotkey_press,
            on_release=self.on_hotkey_release,
        )
        self.control_server.start()

        self._log_startup_diagnostics()

        self.status_item = rumps.MenuItem("Ready")
        self.last_item = rumps.MenuItem("Last: -")
        self.toggle_paste_item = rumps.MenuItem("Paste after transcription", callback=self.toggle_paste)
        self.toggle_paste_item.state = bool(self.config.get("paste_after_transcription", True))

        # LLM-«причёсывание» готового текста. Локальный Qwen выпилен по итогам
        # реального теста (2026-07-28): 5-11с задержки, а форматирования
        # Wispr-уровня 4B-модель стабильно не даёт. Остались «выключено»
        # (по умолчанию) и Claude по подписке для избранных сообщений.
        self.cleaner = TextCleaner(self.config, vocabulary=self.vocabulary)
        self.cleanup_menu = rumps.MenuItem("✨ Причёсывание текста")
        self._cleanup_items: dict[str, rumps.MenuItem] = {}
        for key, label in (
            ("off", "Выключено (быстро, как есть)"),
            ("gemini", "Gemini (~1с, уходит только текст, аудио остаётся на Маке)"),
            ("claude", "Claude на каждое сообщение (по подписке, +10-20с)"),
        ):
            item = rumps.MenuItem(label, callback=self._pick_cleanup_backend)
            self._cleanup_items[key] = item
            self.cleanup_menu.add(item)
        self._apply_cleanup_states()
        self.mode_menu = rumps.MenuItem("✍️ Режим текста (для Gemini)")
        self._mode_items: dict[str, rumps.MenuItem] = {}
        for key, label in MODES.items():
            item = rumps.MenuItem(label, callback=self._pick_mode)
            self._mode_items[key] = item
            self.mode_menu.add(item)
        self._apply_mode_states()

        # History of recent dictations, right in the menu bar: click any entry to
        # copy it back to the clipboard. So a thought is never lost -- you can
        # always grab it here even if the paste went nowhere.
        self.history_size = int(self.config.get("history_size", 20))
        self.history: list[tuple[str, str, str]] = self._load_history()
        self._history_map: dict[int, tuple[str, str, str]] = {}
        self.history_menu = rumps.MenuItem("🗂 История голосовых")
        self._build_history_menu()

        self.menu = [
            self.status_item,
            self.last_item,
            None,
            self.history_menu,
            rumps.MenuItem("↻ Перетранскрибировать последнюю", callback=self.retranscribe_last),
            rumps.MenuItem("✨ Причесать последнее и скопировать", callback=self.cleanup_last),
            None,
            self.cleanup_menu,
            self.mode_menu,
            self.toggle_paste_item,
            rumps.MenuItem("Открыть словарь", callback=self.open_dictionary),
            rumps.MenuItem(
                "Открыть словарь для активного приложения",
                callback=self.open_context_dictionary,
            ),
            rumps.MenuItem("➕ Добавить слово из буфера", callback=self.add_word_from_clipboard),
            rumps.MenuItem("Открыть лог расшифровок", callback=self.open_transcripts),
            rumps.MenuItem("Открыть папку записей", callback=self.open_recordings),
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        # Stay a menu-bar-only app (no Dock icon), matching the previous .app.
        AppHelper.callAfter(self._become_accessory)
        if self.enter_to_stop:
            AppHelper.callAfter(self._install_enter_tap)

    def _become_accessory(self) -> None:
        try:
            from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

            NSApplication.sharedApplication().setActivationPolicy_(
                NSApplicationActivationPolicyAccessory
            )
        except Exception as exc:
            log(f"could not set accessory policy: {exc}")

    def _install_enter_tap(self) -> None:
        """Global key tap so Enter stops a hands-free recording.

        Only swallows Enter while a hands-free recording is active; at all other
        times keystrokes pass through untouched, so normal typing is unaffected."""
        try:
            import Quartz

            RETURN_KEYS = {36, 76}  # Return, keypad Enter

            def callback(proxy, type_, event, refcon):
                try:
                    if type_ in (
                        Quartz.kCGEventTapDisabledByTimeout,
                        Quartz.kCGEventTapDisabledByUserInput,
                    ):
                        Quartz.CGEventTapEnable(self._enter_tap, True)
                        return event
                    if type_ == Quartz.kCGEventKeyDown:
                        keycode = Quartz.CGEventGetIntegerValueField(
                            event, Quartz.kCGKeyboardEventKeycode
                        )
                        if keycode in RETURN_KEYS:
                            with self.event_lock:
                                grab = self.state == "recording" and self.mode == "handsfree"
                            if grab:
                                threading.Thread(target=self.stop_via_enter, daemon=True).start()
                                return None  # swallow so no newline is typed
                except Exception as exc:
                    log(f"enter tap callback error: {exc}")
                return event

            self._enter_tap_cb = callback  # keep a strong reference
            tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionDefault,
                Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown),
                callback,
                None,
            )
            if not tap:
                log("could not create Enter tap (needs Accessibility) -- click-to-stop still works")
                return
            self._enter_tap = tap
            source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            Quartz.CFRunLoopAddSource(
                Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes
            )
            Quartz.CGEventTapEnable(tap, True)
            log("Enter-to-stop tap installed")
        except Exception as exc:
            log(f"could not install Enter tap: {exc}")

    def _log_startup_diagnostics(self) -> None:
        log("Whisper Dictate started")
        try:
            from ApplicationServices import (
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )

            # Passing the prompt option registers this binary in the
            # Accessibility list and shows the system dialog if not yet granted.
            # Once granted it returns True and never prompts again.
            trusted = bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))
            log(f"Accessibility (paste) trusted: {trusted}")
            if not trusted:
                notify(
                    "Whisper Dictate",
                    "Разреши Универсальный доступ для Python, иначе авто-вставка не работает.",
                )
        except Exception as exc:
            log(f"could not query Accessibility trust: {exc}")

    def _load_config(self) -> dict:
        user = PATHS["user_config"]
        default = PATHS["default_config"]
        config = dict(BUILTIN_DEFAULTS)
        try:
            bundled = json.loads(default.read_text(encoding="utf-8"))
            if isinstance(bundled, dict):
                config.update(bundled)
        except (OSError, json.JSONDecodeError) as exc:
            log(f"could not read default config ({exc}); using built-in defaults")
        if user.exists() and user != default:
            try:
                override = json.loads(user.read_text(encoding="utf-8"))
                if not isinstance(override, dict):
                    raise ValueError("top-level value must be an object")
                config.update(override)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                log(f"ignoring invalid user config {user}: {exc}")
        return config

    def _secure_data_paths(self) -> None:
        """Tighten permissions on new and pre-existing private runtime data."""
        private_dir = PATHS["data_dir"]
        recordings_dir = PATHS["recordings_dir"]
        dictionaries_dir = private_dir / "dictionaries"
        private_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        recordings_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        dictionaries_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (private_dir, recordings_dir, dictionaries_dir):
            try:
                os.chmod(directory, 0o700)
            except OSError as exc:
                log(f"could not secure directory {directory}: {exc}")
        private_files = [
            PATHS["user_config"],
            PATHS["data_dir"] / "dictionary.txt",
            self.transcripts_path,
            self.transcripts_jsonl_path,
            PATHS["data_dir"] / "whisper-dictate.lock",
            PATHS["data_dir"] / "whisper-dictate.err.log",
            PATHS["data_dir"] / "whisper-dictate.out.log",
        ]
        private_files.extend(recordings_dir.glob("*.wav"))
        private_files.extend(dictionaries_dir.glob("*.txt"))
        for path in private_files:
            if not path.exists():
                continue
            try:
                os.chmod(path, 0o600)
            except OSError as exc:
                log(f"could not secure file {path}: {exc}")

    def _acquire_single_instance(self) -> None:
        """Refuse to start if another instance already holds the lock.

        Prevents two copies fighting over the same control socket (which used to
        cause lost key events and 'frozen' behaviour)."""
        lock_path = PATHS["data_dir"] / "whisper-dictate.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fh = open(lock_path, "w")
        # Retry briefly: on a restart the previous instance may still be exiting.
        for _attempt in range(20):
            try:
                fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                time.sleep(0.1)
        log("another instance is already running -- exiting")
        sys.exit(0)

    def _save_transcript(
        self,
        result: TranscriptionResult,
        audio_name: str = "",
        source: str = "dictation",
        context: str = "",
    ) -> None:
        """Persist both raw and displayed text before any paste attempt."""
        try:
            self.transcripts_path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            header = f"{stamp} | {audio_name}" if audio_name else stamp
            if result.text:
                with open(self.transcripts_path, "a", encoding="utf-8") as fh:
                    fh.write(f"[{header}]\n{result.text}\n\n")
            record = {
                "timestamp": stamp,
                "source": source,
                "audio": audio_name,
                "raw_text": result.raw_text,
                "text": result.text,
                "warnings": list(result.warnings),
                "backend": result.backend,
                "duration": result.duration,
                "language": result.language,
                "avg_token_probability": result.avg_token_probability,
                "avg_logprob": result.avg_logprob,
                "no_speech_probability": result.no_speech_probability,
                "audio_peak": result.audio_peak,
                "retried": result.retried,
                "context": context,
            }
            with open(self.transcripts_jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._transcripts_since_prune += 1
            if self._transcripts_since_prune >= 25:
                self._prune_transcript_logs()
        except OSError as exc:
            log(f"could not append to transcripts log: {exc}")

    def _prune_transcript_logs(self) -> None:
        if self.keep_transcripts <= 0:
            return
        try:
            if self.transcripts_path.exists():
                content = self.transcripts_path.read_text(encoding="utf-8")
                entries = [
                    match.group(0).strip()
                    for match in re.finditer(
                        r"\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\s*\|\s*[^\]]+)?\]\n.*?(?=\n\[\d{4}-|\Z)",
                        content,
                        re.DOTALL,
                    )
                ]
                if len(entries) > self.keep_transcripts:
                    _atomic_write_text(
                        self.transcripts_path,
                        "\n\n".join(entries[-self.keep_transcripts :]) + "\n\n",
                    )
            if self.transcripts_jsonl_path.exists():
                lines = self.transcripts_jsonl_path.read_text(encoding="utf-8").splitlines()
                if len(lines) > self.keep_transcripts:
                    _atomic_write_text(
                        self.transcripts_jsonl_path,
                        "\n".join(lines[-self.keep_transcripts :]) + "\n",
                    )
            self._transcripts_since_prune = 0
        except OSError as exc:
            log(f"could not prune transcript logs: {exc}")

    def open_dictionary(self, _sender) -> None:
        try:
            self.vocabulary.ensure_file()
            subprocess.run(["open", str(self.vocabulary.path)], check=False)
        except OSError as exc:
            log(f"could not open dictionary: {exc}")

    def add_word_from_clipboard(self, _sender) -> None:
        try:
            import pyperclip

            line = self.vocabulary.add_line(pyperclip.paste())
            log(f"dictionary: word added ({len(line)} chars)")
            notify("Whisper Dictate", f"В словарь добавлено: {line}")
        except Exception as exc:
            notify("Whisper Dictate", f"Не добавил: {exc}")

    def open_context_dictionary(self, _sender) -> None:
        context = self.last_active_context or self._frontmost_app_context()
        if not context:
            notify("Whisper Dictate", "Не удалось определить активное приложение")
            return
        try:
            path = self.vocabulary.ensure_context_file(context)
            if path is not None:
                subprocess.run(["open", str(path)], check=False)
        except OSError as exc:
            log(f"could not open contextual dictionary: {exc}")

    @staticmethod
    def _frontmost_app_context() -> str:
        try:
            from AppKit import NSWorkspace

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return str(app.bundleIdentifier() or app.localizedName() or "").strip()
        except Exception as exc:
            log(f"could not determine frontmost app: {exc}")
            return ""

    def open_transcripts(self, _sender) -> None:
        try:
            self.transcripts_path.parent.mkdir(parents=True, exist_ok=True)
            self.transcripts_path.touch(exist_ok=True)
            subprocess.run(["open", str(self.transcripts_path)], check=False)
        except OSError as exc:
            log(f"could not open transcripts log: {exc}")

    def _load_history(self) -> list[tuple[str, str, str]]:
        """Read the last N dictations from transcripts.txt (newest first) as
        (timestamp, text, audio_filename) -- audio_filename may be '' for old
        entries written before the source file was recorded."""
        try:
            txt = self.transcripts_path.read_text(encoding="utf-8")
        except OSError:
            return []
        entries: list[tuple[str, str, str]] = []
        for m in re.finditer(
            r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:\s*\|\s*([^\]]+))?\]\n(.*?)(?=\n\[\d{4}-|\Z)",
            txt,
            re.DOTALL,
        ):
            body = m.group(3).strip()
            if body:
                entries.append((m.group(1), body, (m.group(2) or "").strip()))
        return entries[-self.history_size :][::-1]  # newest first

    def _build_history_menu(self) -> None:
        try:
            self.history_menu.clear()
        except Exception:
            pass
        self._history_map = {}
        if not self.history:
            self.history_menu.add(rumps.MenuItem("(пока пусто)"))
            return
        for i, (ts, text, wav) in enumerate(self.history, 1):
            preview = text if len(text) <= 55 else text[:52] + "…"
            entry = rumps.MenuItem(f"{i}. {ts[11:16]}  {preview}")
            copy_it = rumps.MenuItem("📋 Скопировать текст", callback=self._hist_action)
            self._history_map[id(copy_it)] = ("copy", text, wav)
            entry.add(copy_it)
            if wav:
                re_it = rumps.MenuItem("↻ Перетранскрибировать из аудио", callback=self._hist_action)
                self._history_map[id(re_it)] = ("retranscribe", text, wav)
                entry.add(re_it)
                open_it = rumps.MenuItem("▶︎ Открыть аудиофайл", callback=self._hist_action)
                self._history_map[id(open_it)] = ("open", text, wav)
                entry.add(open_it)
            self.history_menu.add(entry)

    def _hist_action(self, sender) -> None:
        info = self._history_map.get(id(sender))
        if not info:
            return
        action, text, wav = info
        if action == "copy":
            self.clipboard.copy(text)
            notify("Whisper Dictate", "Скопировано в буфер ✓")
        elif action == "open":
            try:
                subprocess.run(["open", str(PATHS["recordings_dir"] / wav)], check=False)
            except OSError as exc:
                log(f"open audio failed: {exc}")
        elif action == "retranscribe":
            threading.Thread(target=self._retranscribe_wav, args=(wav,), daemon=True).start()

    def _retranscribe_wav(self, wav: str) -> None:
        path = PATHS["recordings_dir"] / wav
        if not path.exists():
            notify("Whisper Dictate", "Аудиофайл уже удалён (ротация записей)")
            return
        notify("Whisper Dictate", "Перетранскрибирую заново…")
        try:
            result = self.transcriber.transcribe_result(path)
            text = result.text
        except Exception as exc:
            log(f"retranscribe failed: {exc}")
            notify("Whisper Dictate", f"Сбой перетранскрибации: {exc}")
            return
        if text:
            self._save_transcript(result, wav, source="retranscription")
            self.clipboard.copy(text)
            self._add_to_history(text, wav)
            notify("Whisper Dictate", f"Готово, в буфере ✓ — {text[:70]}")
        else:
            notify("Whisper Dictate", "Пусто — речь в записи не распознал")

    def retranscribe_last(self, _sender) -> None:
        try:
            files = sorted(PATHS["recordings_dir"].glob("dictation-*.wav"), key=lambda p: p.name)
        except OSError:
            files = []
        if not files:
            notify("Whisper Dictate", "Нет сохранённых записей")
            return
        threading.Thread(target=self._retranscribe_wav, args=(files[-1].name,), daemon=True).start()

    def open_recordings(self, _sender) -> None:
        try:
            PATHS["recordings_dir"].mkdir(parents=True, exist_ok=True)
            subprocess.run(["open", str(PATHS["recordings_dir"])], check=False)
        except OSError as exc:
            log(f"could not open recordings dir: {exc}")

    def _add_to_history(self, text: str, audio_name: str = "") -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.history.insert(0, (stamp, text, audio_name))
        self.history = self.history[: self.history_size]
        AppHelper.callAfter(self._build_history_menu)

    # Interaction model (all transitions under event_lock so concurrent press/
    # release handler threads can never corrupt state):
    #   - HOLD the key           -> push-to-talk; release stops + pastes.
    #   - DOUBLE-CLICK the key    -> hands-free recording; talk without holding.
    #   - while hands-free: CLICK again OR press Enter -> stop + paste.
    #   - a lone single click     -> nothing (recording is discarded).
    # Modes: "hold" | "maybe_double" (waiting to see if a second click lands) |
    #        "handsfree".
    def on_hotkey_press(self, event_time: float | None = None) -> None:
        event_time = event_time if event_time is not None else time.monotonic()
        do_stop = False
        with self.event_lock:
            if self.state == "processing":
                # Раньше нажатия здесь молча игнорировались — если расшифровка
                # зависала, приложение выглядело мёртвым (Fn/Enter не работали).
                # Теперь долгое 'processing' можно сбросить нажатием: состояние
                # возвращается в idle, аудио уже сохранено на диске, а зависший
                # поток довершится или отвалится по таймауту в фоне.
                stuck_for = time.monotonic() - getattr(self, "processing_since", event_time)
                if stuck_for > 45.0:
                    log(f"processing stuck for {stuck_for:.0f}s -- force reset by user press")
                    self._beep("error")
                    notify(
                        "Whisper Dictate",
                        "Сбросил зависшую расшифровку. Аудио сохранено — "
                        "«↻ Перетранскрибировать последнюю» вернёт текст.",
                    )
                    self.mode = "idle"
                    self._set_status("idle", "🎙", "Ready")
                return
            if self.state == "recording":
                if self.mode == "maybe_double":
                    # Second click of a double-click -> switch to hands-free.
                    self._cancel_timer("double_click_timer")
                    self._cancel_timer("start_beep_timer")
                    self.mode = "handsfree"
                    self._beep("start")  # mic is settled by now -> audible on AirPods
                    self._set_status("recording", "🔴", "Запись… клик или Enter — стоп")
                    return
                # hands-free: a click stops. hold: a press here means the release
                # was lost -> recover by stopping instead of deadlocking.
                if self.mode == "hold":
                    log("press while hold-recording (lost release?) -- stopping")
                do_stop = True
            elif self.state == "idle":
                self.press_started_at = event_time
                self.mode = "hold"  # provisional; a quick release re-decides
                self.active_context = self._frontmost_app_context()
                if self.active_context:
                    self.last_active_context = self.active_context
                self._start_recording()
        if do_stop:
            self._stop_and_transcribe()

    def on_hotkey_release(self, event_time: float | None = None) -> None:
        event_time = event_time if event_time is not None else time.monotonic()
        do_stop = False
        with self.event_lock:
            if self.state != "recording" or self.mode in ("handsfree", "maybe_double"):
                # Releases in hands-free / waiting states never stop recording.
                return
            # mode == "hold"
            # Use the time the release reached the control socket, not the time
            # this handler finally acquired event_lock. Opening Bluetooth audio
            # can block for > tap_threshold and used to turn quick taps into holds.
            held_for = event_time - self.press_started_at
            if held_for < self.tap_threshold:
                # A quick tap: maybe the first click of a double-click. Wait for a
                # second click; if none arrives, the recording is discarded. Don't
                # play the start cue yet -- a lone stray tap should be silent.
                self._cancel_timer("start_beep_timer")
                self.mode = "maybe_double"
                self._arm_double_click_timer()
                return
            # A genuine hold -> push-to-talk release stops it.
            do_stop = True
        if do_stop:
            self._stop_and_transcribe()

    def _arm_double_click_timer(self) -> None:
        self._cancel_timer("double_click_timer")
        self.double_click_timer = threading.Timer(self.double_click_window, self._double_click_expired)
        self.double_click_timer.daemon = True
        self.double_click_timer.start()

    def _double_click_expired(self) -> None:
        # No second click arrived -> it was a lone tap. Discard ONLY if nothing
        # was actually said; if the tap caught real speech, keep it and transcribe
        # (never throw away captured voice).
        do_transcribe = False
        with self.event_lock:
            if not (self.state == "recording" and self.mode == "maybe_double"):
                return
            self.double_click_timer = None
            if self.recorder.captured_peak() >= self.hearing_min_level:
                do_transcribe = True
            else:
                self._discard_recording()
        if do_transcribe:
            self._stop_and_transcribe()

    def _discard_recording(self) -> None:
        self._cancel_timer("max_duration_timer")
        self._cancel_timer("start_beep_timer")
        AppHelper.callAfter(self._hide_hud)
        chunker = getattr(self, "live_chunker", None)
        if chunker is not None:
            chunker.cancel()
            self.live_chunker = None
        try:
            self.recorder.abort()
        except Exception as exc:
            log(f"discard recording failed: {exc}")
        self.mode = "idle"
        self.active_context = ""
        self._set_status("idle", "🎙", "Ready")

    def _cancel_timer(self, attr: str) -> None:
        timer = getattr(self, attr, None)
        if timer is not None:
            timer.cancel()
            setattr(self, attr, None)

    def stop_via_enter(self) -> None:
        """Called by the global Enter tap; only stops a hands-free recording."""
        with self.event_lock:
            if not (self.state == "recording" and self.mode == "handsfree"):
                return
        self._stop_and_transcribe()

    def _start_recording(self) -> None:
        try:
            self._start_recorder_watchdogged()
            self._set_status("recording", "🔴", "Recording...")
            self._arm_max_duration_timer()
            # Page the model back into GPU memory while the user is speaking:
            # Metal drops residency after ~3 min idle, which made the first
            # dictation after a break feel much slower.
            threading.Thread(target=self._warm_whisper, daemon=True).start()
            # Open the HTTPS connection for the cleanup pass while the user talks.
            threading.Thread(target=self.cleaner.warmup, daemon=True).start()
            # Живая расшифровка: куски длинной диктовки уходят в whisper прямо
            # во время записи, чтобы после отпускания остался только хвост.
            self.live_chunker = None
            if bool(self.config.get("live_chunking", True)):
                try:
                    self.live_chunker = LiveChunker(
                        self.recorder,
                        self.whisper_server,
                        min_seconds=float(self.config.get("chunk_min_seconds", 40)),
                        max_seconds=float(self.config.get("chunk_max_seconds", 75)),
                    )
                    self.live_chunker.start()
                except Exception as exc:
                    log(f"live chunker failed to start: {exc}")
                    self.live_chunker = None
            if self.hearing_alert:
                threading.Thread(target=self._level_monitor, daemon=True).start()
            # Play the start cue a beat AFTER opening the mic: by then AirPods have
            # finished switching to hands-free mode (which briefly mutes output and
            # swallowed the old immediate beep). Cancelled if the press turns out to
            # be a short tap that gets discarded. Only Bluetooth mics need that
            # long a wait -- with the built-in mic the cue fires almost at once.
            self._cancel_timer("start_beep_timer")
            # Only Bluetooth headsets swallow a cue played while their mic spins
            # up, so they are the exception that needs the long delay. Built-in
            # mics ("Микрофон iMac", "MacBook Pro Microphone") and wired/USB ones
            # get the fast cue.
            device = self.recorder.input_device_name().lower()
            bluetooth = any(
                marker in device
                for marker in ("airpods", "bluetooth", "beats", "buds", "headset", "hands-free", "bt")
            )
            beep_delay = self.tap_threshold if bluetooth else float(
                self.config.get("start_beep_delay_builtin", 0.15)
            )
            self.start_beep_timer = threading.Timer(beep_delay, lambda: self._beep("start"))
            self.start_beep_timer.daemon = True
            self.start_beep_timer.start()
        except Exception as exc:
            self.active_context = ""
            self._set_status("idle", "🎙", "Ready")
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            self._beep("error")
            notify("Whisper Dictate", f"Recording failed: {exc}")

    def _warm_whisper(self) -> None:
        """Best-effort model warm-up, fired when recording starts (see
        WhisperEngine.warmup). Runs a tiny silent file through the persistent
        server so the weights are GPU-resident by the time the key is released.
        """
        try:
            path = PATHS["data_dir"] / "warmup.wav"
            if not path.exists():
                with wave.open(str(path), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(16000)
                    w.writeframes(b"\x00\x00" * 3200)  # 0.2 s of silence
            self.whisper_server.warmup(path)
        except Exception:
            pass

    def _arm_max_duration_timer(self) -> None:
        if self.max_duration_timer is not None:
            self.max_duration_timer.cancel()
        if self.max_recording_seconds <= 0:
            return
        self.max_duration_timer = threading.Timer(self.max_recording_seconds, self._max_duration_reached)
        self.max_duration_timer.daemon = True
        self.max_duration_timer.start()

    @staticmethod
    def _kill_orphan_whisper_servers() -> None:
        """Убить whisper-server'ы, оставшиеся без родителя (PPID=1).

        После аварийного перезапуска (kill -9, os._exit) дочерний сервер
        выживает сиротой и держит ~1.8 ГБ; новый экземпляр поднимает своего,
        и они копятся. Чистим при старте."""
        try:
            out = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,comm="],
                stdout=subprocess.PIPE, text=True, timeout=10,
            ).stdout
            for line in out.splitlines():
                parts = line.split(None, 2)
                if len(parts) == 3 and parts[1] == "1" and parts[2].endswith("whisper-server"):
                    os.kill(int(parts[0]), signal.SIGKILL)
                    log(f"killed orphaned whisper-server (pid {parts[0]})")
        except Exception as exc:
            log(f"orphan sweep failed: {exc}")

    def _start_recorder_watchdogged(self, timeout: float = 8.0) -> None:
        """recorder.start() под сторожевым таймером.

        Открытие микрофона (PortAudio/CoreAudio) может зависнуть намертво —
        особенно после того, как предыдущий поток пришлось бросить зависшим.
        Этот вызов происходит под event_lock, так что вечный start() делал
        приложение полностью глухим (36 потоков в очереди, 2026-08-07).
        Зависли — честно перезапускаемся: launchd поднимет нас за секунды со
        свежим PortAudio."""
        done = threading.Event()
        errors: list[BaseException] = []

        def _do_start() -> None:
            try:
                self.recorder.start()
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        threading.Thread(target=_do_start, daemon=True).start()
        if not done.wait(timeout):
            log(f"recorder.start() hung > {timeout:.0f}s -- PortAudio wedged, restarting app")
            self._beep("error")
            notify(
                "Whisper Dictate",
                "Микрофон завис — перезапускаюсь. Повтори диктовку через 5 секунд.",
            )
            threading.Thread(target=self.whisper_server.stop, daemon=True).start()
            time.sleep(1.5)
            os._exit(1)  # ненулевой код -> launchd перезапустит (KeepAlive)
        if errors:
            raise errors[0]

    def _max_duration_reached(self) -> None:
        log(f"max recording duration ({self.max_recording_seconds:.0f}s) reached -- auto-stopping")
        self._stop_and_transcribe()

    def _beep(self, name: str) -> None:
        if self.sounds:
            play_sound(name)

    # --- "we can't hear you" on-screen warning -------------------------------

    def _level_monitor(self) -> None:
        """While recording, watch the live mic level. If it stays near-silent for
        a couple of seconds, throw a big warning over all windows so the user
        stops talking into a dead mic instead of losing a long monologue."""
        time.sleep(1.0)  # let the mic spin up (AirPods take ~1s to switch on)
        quiet_since: float | None = None
        noise_floor: float | None = None
        heard_speech = False
        shown = False
        while self.state == "recording":
            level = self.recorder.current_level
            now = time.monotonic()
            if level > 0:
                noise_floor = level if noise_floor is None else min(noise_floor, level)
            adaptive_level = self.hearing_level
            if noise_floor is not None:
                adaptive_level = min(
                    self.hearing_level,
                    max(self.hearing_min_level, noise_floor * 4.0),
                )
            callback_stalled = (now - self.recorder.last_callback_at) > 1.5
            if level >= adaptive_level and not callback_stalled:
                heard_speech = True
                quiet_since = None
                if shown:
                    shown = False
                    AppHelper.callAfter(self._hide_hud)
            else:
                if quiet_since is None:
                    quiet_since = now
                alert_after = (
                    self.hearing_pause_alert_after if heard_speech else self.hearing_alert_after
                )
                if callback_stalled:
                    alert_after = min(alert_after, 2.0)
                if (now - quiet_since) >= alert_after and not shown:
                    shown = True
                    log(
                        "mic near-silent "
                        f"(level={level:.4f}, threshold={adaptive_level:.4f}, "
                        f"callback_stalled={callback_stalled}) -- showing warning"
                    )
                    AppHelper.callAfter(self._show_hud)
            time.sleep(0.2)
        AppHelper.callAfter(self._hide_hud)

    def _ensure_hud(self) -> None:
        if self._hud is not None:
            return
        from AppKit import (
            NSPanel, NSColor, NSTextField, NSFont, NSBackingStoreBuffered, NSMakeRect,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorStationary,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSScreenSaverWindowLevel, NSTextAlignmentCenter,
        )

        w, h = self._hud_size
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, w, h), 0, NSBackingStoreBuffered, False
        )
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setLevel_(NSScreenSaverWindowLevel)
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        content = panel.contentView()
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(18.0)
        content.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.86, 0.12, 0.12, 0.95).CGColor()
        )
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(10, 22, w - 20, 52))
        # Reassure that recording continues -- this is a "speak up" nudge, not a stop.
        label.setStringValue_("🔇  Тебя не слышно — говори громче (запись идёт)")
        label.setEditable_(False)
        label.setSelectable_(False)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setTextColor_(NSColor.whiteColor())
        label.setFont_(NSFont.boldSystemFontOfSize_(22.0))
        label.setAlignment_(NSTextAlignmentCenter)
        content.addSubview_(label)
        self._hud = panel

    def _show_hud(self) -> None:
        try:
            from AppKit import NSScreen

            self._ensure_hud()
            frame = NSScreen.mainScreen().frame()
            w, h = self._hud_size
            x = frame.origin.x + (frame.size.width - w) / 2.0
            y = frame.origin.y + frame.size.height - h - 120.0
            self._hud.setFrameOrigin_((x, y))
            self._hud.orderFrontRegardless()
        except Exception as exc:
            log(f"HUD show failed ({exc}) -- falling back to notification")
            notify("Whisper Dictate", "🔇 Тебя не слышно — микрофон молчит, запись пустая!")

    def _hide_hud(self) -> None:
        try:
            if self._hud is not None:
                self._hud.orderOut_(None)
        except Exception as exc:
            log(f"HUD hide failed: {exc}")

    def _stop_and_transcribe(self) -> None:
        with self.stop_lock:
            if self.state != "recording":
                return
            # Immediate "recording stopped" cue the moment the key is released.
            t_release = time.monotonic()
            self._beep("stop")
            AppHelper.callAfter(self._hide_hud)
            self._cancel_timer("max_duration_timer")
            self._cancel_timer("double_click_timer")
            self._cancel_timer("start_beep_timer")

            self._set_status("processing", "⏳", "Transcribing...")
            self.processing_since = time.monotonic()
            context = self.active_context
            # Microseconds in the name so two recordings that stop in the same
            # second don't overwrite each other (which lost the failing audio).
            audio_path = PATHS["recordings_dir"] / f"dictation-{datetime.now():%Y%m%d-%H%M%S-%f}.wav"
            ok = False
            try:
                chunker = getattr(self, "live_chunker", None)
                self.live_chunker = None
                self.recorder.stop(audio_path)
                t_asr = time.monotonic()
                result = None
                if chunker is not None:
                    live = chunker.finish()
                    if live is not None:
                        try:
                            result = self._finalize_live(audio_path, live, context)
                            log(
                                f"live chunks: {len(live[0])} + tail, "
                                f"{len(result.text)} chars"
                            )
                        except Exception as exc:
                            log(f"live stitching failed ({exc}); full re-transcribe")
                            result = None
                if result is None:
                    result = self.transcriber.transcribe_result(audio_path, context=context)
                text = result.text
                log(
                    f"ASR: {round(result.duration or 0)}s audio -> {len(text)} chars "
                    f"in {time.monotonic() - t_asr:.1f}s ({result.backend})"
                )
                self._save_transcript(result, audio_path.name, context=context)
                if text and self.cleaner.backend != "off":
                    # Сырой текст уже сохранён в transcripts + история аудио —
                    # чистка может только улучшить то, что уходит в буфер.
                    self._set_status("processing", "✨", "Полирую текст...")
                    t_clean = time.monotonic()
                    cleaned, used = self.cleaner.clean(text, context=context)
                    if used is not None:
                        text = cleaned
                        model = f" {self.cleaner.last_model}" if used == "gemini" else ""
                        log(f"cleanup ({used}{model}, {self.cleaner.mode}): {time.monotonic() - t_clean:.1f}s")
                    else:
                        log(f"cleanup skipped/failed: {time.monotonic() - t_clean:.1f}s, raw text kept")
                if text:
                    # Save first: the thought is preserved on disk, in the
                    # menu-bar history, and on the clipboard BEFORE we attempt the
                    # (fallible) auto-paste.
                    self.last_transcription = text
                    self._add_to_history(text, audio_path.name)
                    if bool(self.config.get("paste_after_transcription", True)):
                        self.clipboard.paste_text(
                            text,
                            restore=bool(self.config.get("restore_clipboard", True)),
                            restore_delay=float(self.config.get("restore_clipboard_delay", 0.8)),
                        )
                    else:
                        self.clipboard.copy(text)
                    log(f"release->paste: {time.monotonic() - t_release:.1f}s")
                    self._update_last_item(text)
                    ok = True
                else:
                    log(f"empty transcription for {audio_path.name}")
                    notify(
                        "Whisper Dictate",
                        "Пусто — ничего не распознал. Аудио сохранено, попробуй ещё раз.",
                    )
            except Exception as exc:
                log(f"transcription failed: {exc}")
                traceback.print_exc(file=sys.stderr)
                sys.stderr.flush()
                notify(
                    "Whisper Dictate",
                    f"Сбой расшифровки (аудио сохранено: {audio_path.name}): {exc}",
                )
            finally:
                self._beep("done" if ok else "error")
                self._prune_recordings()
                # Если пользователь сбросил зависшую расшифровку и уже начал
                # новую запись, этот (опоздавший) поток не должен затирать её
                # состояние.
                with self.event_lock:
                    if self.state == "processing":
                        self.mode = "idle"
                        self.active_context = ""
                        self._set_status("idle", "🎙", "Ready")

    def _finalize_live(self, audio_path: Path, live: tuple[list[str], int], context: str) -> TranscriptionResult:
        """Собрать финальный текст из живых чанков + хвоста после последнего разреза.

        Хвост берётся из уже сохранённого полного WAV (это и страховка: если
        здесь что-то падает, вызывающий код просто расшифровывает весь файл)."""
        texts, cut_samples = live
        with wave.open(str(audio_path), "rb") as wav:
            rate = wav.getframerate()
            total = wav.getnframes()
            start = min(cut_samples, total)
            wav.setpos(start)
            tail_bytes = wav.readframes(total - start)
        tail_text = ""
        if len(tail_bytes) > rate:  # больше ~0.5 сек — есть что расшифровывать
            tail_path = PATHS["recordings_dir"] / f".live-tail-{audio_path.stem}.wav"
            try:
                with wave.open(str(tail_path), "wb") as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(rate)
                    out.writeframes(tail_bytes)
                # Хвост до chunk_max (75с) — одним запросом в ТЁПЛЫЙ сервер:
                # transcribe_detailed для 60-75с уходил в холодный CLI (+5с
                # ожидания). Длиннее бывает только если чанкер отстал — тогда
                # полный движок сам порежет.
                tail_seconds = len(tail_bytes) / 2 / rate
                if tail_seconds <= float(self.config.get("chunk_max_seconds", 75)) + 5:
                    tail_text = self.whisper_server.transcribe_live_chunk(tail_path)
                else:
                    tail_text = self.whisper_server.transcribe_detailed(tail_path).text
            finally:
                try:
                    tail_path.unlink()
                except OSError:
                    pass
        raw = " ".join(t for t in [*texts, tail_text] if t).strip()
        detailed = WhisperResponse(
            text=raw,
            backend="live-chunked",
            duration=_wav_duration(audio_path),
            audio_peak=_wav_peak(audio_path),
        )
        return self.transcriber.finalize_raw(raw, detailed, context=context)

    def _prune_recordings(self) -> None:
        """Keep only the most recent N dictation files (filenames sort chronologically)."""
        if self.keep_recordings <= 0:
            return
        try:
            files = sorted(PATHS["recordings_dir"].glob("dictation-*.wav"), key=lambda p: p.name)
        except OSError:
            return
        for old in files[: -self.keep_recordings]:
            try:
                old.unlink()
            except OSError:
                pass

    def _set_status(self, state: str, title: str, label: str) -> None:
        self.state = state
        AppHelper.callAfter(self._apply_status, title, label)

    def _apply_status(self, title: str, label: str) -> None:
        self.title = title
        self.status_item.title = label

    def _update_last_item(self, text: str) -> None:
        preview = text if len(text) <= 50 else text[:47] + "..."
        self.last_item.title = f"Last: {preview}"

    # --- LLM-причёсывание -----------------------------------------------------

    def _apply_cleanup_states(self) -> None:
        current = self.cleaner.backend
        for key, item in self._cleanup_items.items():
            item.state = key == current

    def _pick_cleanup_backend(self, sender) -> None:
        for key, item in self._cleanup_items.items():
            if item is sender:
                self.cleaner.backend = key
                break
        self._apply_cleanup_states()
        _atomic_write_json(PATHS["user_config"], self.config)

    def _apply_mode_states(self) -> None:
        current = self.cleaner.mode
        for key, item in self._mode_items.items():
            item.state = key == current

    def _pick_mode(self, sender) -> None:
        for key, item in self._mode_items.items():
            if item is sender:
                self.cleaner.mode = key
                break
        self._apply_mode_states()
        _atomic_write_json(PATHS["user_config"], self.config)

    def cleanup_last(self, _sender) -> None:
        """Причесать последний транскрипт через Claude и положить в буфер."""
        text = self.last_transcription
        if not text:
            notify("Whisper Dictate", "Нечего причёсывать — последней расшифровки нет.")
            return

        def run() -> None:
            self._set_status("processing", "✨", "Полирую текст...")
            cleaned, used = self.cleaner.clean(text, backend="claude")
            self._set_status("idle", "🎙", "Ready")
            if used is None:
                notify("Whisper Dictate", "Причесать не вышло — оставил как есть (текст в буфере).")
                self.clipboard.copy(text)
                return
            self.clipboard.copy(cleaned)
            self.last_transcription = cleaned
            self._add_to_history(cleaned, "")
            notify("Whisper Dictate", f"✨ Готово ({used}) — текст в буфере, жми Cmd+V.")

        threading.Thread(target=run, daemon=True).start()

    def toggle_paste(self, sender) -> None:
        self.config["paste_after_transcription"] = not bool(self.config.get("paste_after_transcription", True))
        sender.state = self.config["paste_after_transcription"]
        _atomic_write_json(PATHS["user_config"], self.config)

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
