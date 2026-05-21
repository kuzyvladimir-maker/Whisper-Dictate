from __future__ import annotations

import subprocess
import time

import pyperclip
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    kCGEventFlagMaskCommand,
    kCGHIDEventTap,
)


V_KEY_CODE = 9


class ClipboardPaste:
    def copy(self, text: str) -> None:
        pyperclip.copy(text)

    def paste(self) -> None:
        time.sleep(0.05)
        down = CGEventCreateKeyboardEvent(None, V_KEY_CODE, True)
        up = CGEventCreateKeyboardEvent(None, V_KEY_CODE, False)
        CGEventSetFlags(down, kCGEventFlagMaskCommand)
        CGEventSetFlags(up, kCGEventFlagMaskCommand)
        CGEventPost(kCGHIDEventTap, down)
        CGEventPost(kCGHIDEventTap, up)


def notify(title: str, message: str) -> None:
    def quote(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    script = f"display notification {quote(message)} with title {quote(title)}"
    subprocess.run(["osascript", "-e", script], check=False)
