# Whisper Dictate

Local, offline macOS dictation. Hold `Fn` to record, release to transcribe and paste. Runs entirely on-device, no cloud, no subscription.

## What it does

- **Hold `Fn` / Globe**: record while held.
- **Release `Fn`**: stop, transcribe locally, paste with `Cmd+V`.
- **Double-tap `Fn`**: start recording without holding. Tap `Fn` again to stop.

Menu bar states: `🎙` ready · `🔴` recording · `⏳` transcribing.

Powered by [whisper.cpp](https://github.com/ggml-org/whisper.cpp) with Metal acceleration on Apple Silicon. Default model is `large-v3-turbo` (~1.6 GB). Karabiner-Elements is required to capture the `Fn` key, which macOS does not expose as a global hotkey to regular apps.

## Install — for end users

1. Install **Karabiner-Elements**: <https://karabiner-elements.pqrs.org>
2. Double-click `WhisperDictate-x.y.z.pkg`. macOS will warn that the developer is unverified — right-click the .pkg → **Open** → **Open**.
3. After install, the System Settings panels for Microphone and Accessibility will open automatically. Allow both for **Whisper Dictate**.
4. Open Karabiner-Elements → **Complex Modifications** → **Add rule** → enable **Whisper Dictate: Fn/Globe to F18**.
5. Done. The app auto-starts on login. Hold `Fn` in any text field to dictate.

## Install — for developers (build from source)

```bash
bash install.sh
```

This installs Python 3.11, builds whisper.cpp with Metal, downloads the model, builds the `.app` bundle, and registers the LaunchAgent for the local `dist/Whisper Dictate.app`. After that, grant Microphone + Accessibility in System Settings and enable the Karabiner rule.

To produce a `.pkg` installer for distribution:

```bash
bash build_pkg.sh
```

Outputs `dist/WhisperDictate-x.y.z.pkg` (~1.4 GB, includes the model).

## Configuration

User config lives at `~/Library/Application Support/Whisper Dictate/config.json` (auto-created on first toggle from the menu). Bundle default is at `Contents/Resources/config.json`.

- `language` — `auto`, `en`, `ru`, etc.
- `paste_after_transcription` — auto-paste or copy-only.
- `tap_threshold_seconds`, `double_tap_window_seconds` — tap/hold timing.
- `initial_prompt` — Whisper prompt to bias style.
- `model_filename` — to swap models, drop a different `ggml-*.bin` into `Contents/Resources/models/` and update this field.

## Where things live

- **Bundle**: `/Applications/Whisper Dictate.app` (installed) or `dist/Whisper Dictate.app` (dev).
- **Model + binaries**: inside the bundle (`Contents/Resources/models/`, `Contents/Resources/whisper-cli-bin/`).
- **Recordings + logs + user config**: `~/Library/Application Support/Whisper Dictate/`.
- **LaunchAgent**: `~/Library/LaunchAgents/com.whisperdictate.app.plist`.
- **Karabiner rule**: `~/.config/karabiner/assets/complex_modifications/whisper_fn_to_f18.json`.

## FAQ

| Question | Answer |
|---|---|
| Internet required? | Only at install time, to download the model and Karabiner-Elements. Dictation itself is fully offline. |
| Max length? | No practical limit. whisper.cpp processes any duration; 5+ minute dictations work fine. |
| Cost? | Free. MIT-licensed code, free models, no cloud calls. |
| Privacy? | Audio never leaves your machine. |

## Uninstall

```bash
bash uninstall.sh
```

Removes the LaunchAgent, Karabiner rule, and installed bundle. Data (recordings, user config) is kept; set `PURGE_DATA=1` to wipe it too.

## Troubleshooting

- **Nothing happens on `Fn`**: check `/var/log/karabiner/core_service.log` for rule errors. The rule must be enabled in Karabiner UI.
- **Records but does not paste**: Accessibility permission for `Whisper Dictate.app` is missing or stale. After a rebuild the bundle's signature changes — remove the old Accessibility entry and re-add the bundle.
- **Errors**: see `~/Library/Application Support/Whisper Dictate/whisper-dictate.err.log` (installed) or `whisper-dictate.err.log` in the project folder (dev).
- **Logitech / third-party keyboards**: `Fn` may be handled inside the keyboard and never reach macOS. Map another key (right Option, spare F-key) to the same trigger in Karabiner.
