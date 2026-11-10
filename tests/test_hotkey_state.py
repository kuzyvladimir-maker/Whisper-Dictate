from __future__ import annotations

import threading
import unittest

from main import WhisperDictateApp


def make_app() -> WhisperDictateApp:
    app = WhisperDictateApp.__new__(WhisperDictateApp)
    app.event_lock = threading.Lock()
    app.state = "idle"
    app.mode = "idle"
    app.tap_threshold = 0.4
    app.press_started_at = 0.0
    app.double_click_timer = None
    app.start_beep_timer = None
    app.stop_called = False
    app.double_timer_called = False
    app.active_context = ""
    app.last_active_context = ""
    app._frontmost_app_context = lambda: "com.microsoft.VSCode"
    app._start_recording = lambda: setattr(app, "state", "recording")
    app._stop_and_transcribe = lambda: setattr(app, "stop_called", True)
    app._arm_double_click_timer = lambda: setattr(app, "double_timer_called", True)
    return app


class HotkeyStateTests(unittest.TestCase):
    def test_quick_tap_uses_arrival_timestamps(self) -> None:
        app = make_app()
        app.on_hotkey_press(event_time=100.0)
        # Handler execution can be delayed; the physical release timestamp stays 100.05.
        app.on_hotkey_release(event_time=100.05)
        self.assertFalse(app.stop_called)
        self.assertEqual(app.mode, "maybe_double")
        self.assertTrue(app.double_timer_called)
        self.assertEqual(app.last_active_context, "com.microsoft.VSCode")

    def test_hold_stops_recording(self) -> None:
        app = make_app()
        app.on_hotkey_press(event_time=100.0)
        app.on_hotkey_release(event_time=100.6)
        self.assertTrue(app.stop_called)


if __name__ == "__main__":
    unittest.main()
