from __future__ import annotations

import subprocess
import threading
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

    def paste_text(self, text: str, restore: bool = True, restore_delay: float = 0.8) -> None:
        """Put text on the clipboard, Cmd+V, then give the previous clipboard back
        (as in macbook/ Lite). The text stays reachable via the history menu. If the
        user copies something else in the meantime, their copy wins."""
        saved = _snapshot_clipboard() if restore else None
        _set_transient_text(text)
        change_count = _change_count()
        self.paste()
        if saved is None:
            return

        def _restore() -> None:
            time.sleep(restore_delay)
            if _change_count() == change_count:
                _restore_clipboard(saved)

        threading.Thread(target=_restore, daemon=True).start()

    def paste(self) -> None:
        time.sleep(0.05)
        down = CGEventCreateKeyboardEvent(None, V_KEY_CODE, True)
        up = CGEventCreateKeyboardEvent(None, V_KEY_CODE, False)
        CGEventSetFlags(down, kCGEventFlagMaskCommand)
        CGEventSetFlags(up, kCGEventFlagMaskCommand)
        CGEventPost(kCGHIDEventTap, down)
        CGEventPost(kCGHIDEventTap, up)


def _pasteboard():
    from AppKit import NSPasteboard

    return NSPasteboard.generalPasteboard()


def _change_count() -> int:
    try:
        return int(_pasteboard().changeCount())
    except Exception:
        return -1


def _snapshot_clipboard() -> list[list[tuple[str, bytes]]] | None:
    try:
        items = []
        for item in _pasteboard().pasteboardItems() or []:
            pairs = []
            for t in item.types():
                data = item.dataForType_(t)
                if data is not None:
                    pairs.append((str(t), bytes(data)))
            items.append(pairs)
        return items
    except Exception:
        return None


def _set_transient_text(text: str) -> None:
    """Dictated text marked transient so clipboard managers skip it."""
    try:
        from AppKit import NSPasteboardItem
        from Foundation import NSData

        pb = _pasteboard()
        pb.clearContents()
        item = NSPasteboardItem.alloc().init()
        item.setString_forType_(text, "public.utf8-plain-text")
        item.setData_forType_(NSData.data(), "org.nspasteboard.TransientType")
        pb.writeObjects_([item])
    except Exception:
        pyperclip.copy(text)


def _restore_clipboard(saved: list[list[tuple[str, bytes]]]) -> None:
    try:
        from AppKit import NSPasteboardItem
        from Foundation import NSData

        pb = _pasteboard()
        pb.clearContents()
        if not saved:
            return
        items = []
        for pairs in saved:
            item = NSPasteboardItem.alloc().init()
            for t, data in pairs:
                item.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), t)
            items.append(item)
        pb.writeObjects_(items)
    except Exception:
        pass


def notify(title: str, message: str) -> None:
    def quote(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    script = f"display notification {quote(message)} with title {quote(title)}"
    subprocess.run(["osascript", "-e", script], check=False)


_SOUNDS = {
    "start": "/System/Library/Sounds/Ping.aiff",   # запись началась
    "stop": "/System/Library/Sounds/Pop.aiff",     # запись остановлена (отпустил)
    "done": "/System/Library/Sounds/Glass.aiff",   # текст расшифрован и вставлен
    "error": "/System/Library/Sounds/Basso.aiff",  # что-то пошло не так
}


def play_sound(name: str) -> None:
    """Fire-and-forget audible cue. Never blocks, never raises."""
    path = _SOUNDS.get(name)
    if not path:
        return
    try:
        subprocess.Popen(
            ["afplay", "-v", "2", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass
