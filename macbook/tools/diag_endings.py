#!/usr/bin/env python3
"""Where do the last words go? Runs on the Mac with Whisper Dictate Lite installed.

Prints numbers only: no audio and no dictated text leave the machine.

Part A (offline): input level of each saved recording, as percentiles of 50 ms RMS windows,
plus the RMS profile of the last 2 s in 200 ms steps. Shows whether the mic level is low and
whether sound is still going on at the very end of the file (capture cut) or not.

Part B (uses the Gemini keys, 2 requests per file): the same recording sent as is and
normalised (peak to 0.9, gain at most x18, like the iMac version does before Whisper).
Compares word counts and whether the normalised text has extra words at the END. If quiet
endings are lost by recognition, "extra_end" will be > 0 for the normalised copy.

Usage:
  /usr/bin/python3 diag_endings.py --since 13:29 --until 14:20 --max 8
"""
import argparse
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import wave

REC_DIR = os.path.expanduser("~/Library/Application Support/WhisperDictateLite/recordings")
APP = os.path.expanduser("~/Applications/Whisper Dictate Lite.app/Contents/MacOS/WhisperDictateLite")


def read(path):
    w = wave.open(path)
    sr, n, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
    raw = w.readframes(n)
    w.close()
    if sw != 2:
        raise ValueError("expected 16-bit WAV")
    x = list(struct.unpack("<%dh" % (n * ch), raw))
    if ch > 1:
        x = x[::ch]
    return sr, x


def rms_windows(x, sr, seconds):
    win = max(1, int(sr * seconds))
    out = []
    for i in range(0, len(x) - win + 1, win):
        acc = 0
        for v in x[i:i + win]:
            acc += v * v
        out.append(math.sqrt(acc / win) / 32768.0)
    return out


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))] if s else 0.0


def words(text):
    return re.findall(r"[\wёЁ-]+", text.lower())


def transcribe(app, path):
    out = subprocess.run([app, "--bench", "--model", "gemini-3.5-flash-lite", path],
                         capture_output=True, text=True, timeout=180).stdout.splitlines()
    text, ok = [], False
    for line in out:
        if "→" in line and os.path.basename(path) in line:
            ok = True
            continue
        if ok and line.startswith("   ") and not line.startswith("   ·"):
            text.append(line.strip())
        elif ok and text:
            break
    return words(" ".join(text)) if ok else None


def extra_end_words(base, other):
    """Words `other` has after the last place where `base`'s final two words appear
    (falls back to the final word). None when the ending cannot be aligned."""
    if not base or not other:
        return None
    for k in (2, 1):
        if len(base) < k:
            continue
        tail = base[-k:]
        for i in range(len(other) - k, -1, -1):
            if other[i:i + k] == tail:
                return len(other) - (i + k)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="00:00")
    ap.add_argument("--until", default="23:59")
    ap.add_argument("--max", type=int, default=8, help="files for part B (2 Gemini requests each)")
    ap.add_argument("--no-gemini", action="store_true")
    ap.add_argument("--app", default=APP)
    args = ap.parse_args()

    files = []
    for name in sorted(os.listdir(REC_DIR)):
        if not name.endswith(".wav"):
            continue
        p = os.path.join(REC_DIR, name)
        hm = time.strftime("%H:%M", time.localtime(os.path.getmtime(p)))
        if args.since <= hm <= args.until:
            files.append((hm, p))
    print("files in window:", len(files))

    print("A) time  dur   peak   rms50  rms90  rms99 | last 2 s, RMS per 200 ms (oldest -> newest)")
    levels = {}
    for hm, p in files:
        sr, x = read(p)
        r = rms_windows(x, sr, 0.05)
        tail = rms_windows(x[-2 * sr:], sr, 0.2)
        peak = max(abs(v) for v in x) / 32768.0 if x else 0
        levels[p] = peak
        print("   %s %5.1fs  %.3f  %.3f  %.3f  %.3f | %s" % (
            hm, len(x) / float(sr), peak, pct(r, 0.5), pct(r, 0.9), pct(r, 0.99),
            " ".join("%.3f" % v for v in tail)))

    if args.no_gemini:
        return
    print("B) time  words_as_is  words_normalised  gain  extra_end(norm vs as-is)  extra_end(as-is vs norm)")
    tmpdir = tempfile.mkdtemp(prefix="wdl-diag-")
    try:
        for hm, p in files[-args.max:]:
            sr, x = read(p)
            gain = min(18.0, 0.9 / max(levels[p], 1e-6))
            y = [max(-32768, min(32767, int(v * gain))) for v in x]
            q = os.path.join(tmpdir, "norm-" + os.path.basename(p))
            w = wave.open(q, "wb")
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(struct.pack("<%dh" % len(y), *y))
            w.close()
            a = transcribe(args.app, p)
            b = transcribe(args.app, q)
            os.remove(q)
            if a is None or b is None:
                print("   %s  request failed" % hm)
                continue
            fmt_n = lambda v: "?" if v is None else str(v)
            print("   %s  %4d  %4d  x%.1f  %s  %s" % (hm, len(a), len(b), gain,
                                                   fmt_n(extra_end_words(a, b)), fmt_n(extra_end_words(b, a))))
    finally:
        for f in os.listdir(tmpdir):
            os.remove(os.path.join(tmpdir, f))
        os.rmdir(tmpdir)


if __name__ == "__main__":
    sys.exit(main())
