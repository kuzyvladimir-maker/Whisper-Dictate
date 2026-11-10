#!/usr/bin/env bash
# Удаляет Whisper Dictate Lite. Настройки, ключи и словарь (~/.config/whisper-dictate)
# остаются; с --purge удаляются и они, и локальная модель.
set -euo pipefail

bundle_id="com.whisperdictate.lite"
launchctl bootout "gui/$(id -u)/$bundle_id" >/dev/null 2>&1 || true
pkill -x WhisperDictateLite >/dev/null 2>&1 || true
rm -f "$HOME/Library/LaunchAgents/$bundle_id.plist"
rm -rf "$HOME/Applications/Whisper Dictate Lite.app"
tccutil reset All "$bundle_id" >/dev/null 2>&1 || true

if [ "${1:-}" = "--purge" ]; then
  rm -f "$HOME/.config/whisper-dictate/env" "$HOME/.config/whisper-dictate/lite.json" \
        "$HOME/.config/whisper-dictate/dictionary.txt"
  rmdir "$HOME/.config/whisper-dictate" 2>/dev/null || true
  rm -rf "$HOME/.local/share/whisper-dictate"
  rm -f "$HOME/Library/Logs/WhisperDictateLite"*.log
fi
echo "Whisper Dictate Lite удалён."
