#!/usr/bin/env bash
set -euo pipefail

PLIST_PATH="$HOME/Library/LaunchAgents/com.whisperdictate.app.plist"
KARABINER_RULE="$HOME/.config/karabiner/assets/complex_modifications/whisper_fn_to_f18.json"
DATA_DIR="$HOME/Library/Application Support/Whisper Dictate"
INSTALLED_BUNDLE="/Applications/Whisper Dictate.app"

launchctl bootout "gui/$(id -u)/com.whisperdictate.app" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"
rm -f "$KARABINER_RULE"

if [ -d "$INSTALLED_BUNDLE" ]; then
  rm -rf "$INSTALLED_BUNDLE"
  printf 'Removed installed bundle: %s\n' "$INSTALLED_BUNDLE"
fi

if [ "${PURGE_DATA:-}" = "1" ] && [ -d "$DATA_DIR" ]; then
  rm -rf "$DATA_DIR"
  printf 'Removed data dir: %s\n' "$DATA_DIR"
fi

printf 'LaunchAgent and Karabiner rule removed.\n'
printf 'Re-enable the rule manually in Karabiner-Elements UI if you reinstall.\n'
printf 'Data and logs in %s are kept. Re-run with PURGE_DATA=1 to delete them.\n' "$DATA_DIR"
