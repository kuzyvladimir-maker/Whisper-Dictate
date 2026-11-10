#!/bin/bash
# Sends "press" or "release" to the Whisper Dictate control socket.
# Invoked by Karabiner-Elements on Fn key events.
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_CONFIG="$SCRIPT_DIR/config.json"
USER_CONFIG="$HOME/Library/Application Support/Whisper Dictate/config.json"

# Source checkouts use their local config; an installed bundle uses the user
# override first and then Contents/Resources/config.json.
if [[ "$SCRIPT_DIR" == *"/Contents/Resources"* ]]; then
  CONFIG="$USER_CONFIG"
  [ -r "$CONFIG" ] || CONFIG="$DEFAULT_CONFIG"
else
  CONFIG="$SCRIPT_DIR/config.local.json"
  [ -r "$CONFIG" ] || CONFIG="$DEFAULT_CONFIG"
fi

SOCKET="/tmp/whisper-dictate.sock"
if [ -r "$CONFIG" ]; then
  CONFIGURED=$(/usr/bin/plutil -extract control_socket raw -o - "$CONFIG" 2>/dev/null || true)
  [ -n "$CONFIGURED" ] && SOCKET="$CONFIGURED"
fi

case "${1:-}" in
  press|release) exec /usr/bin/nc -U -w 1 "$SOCKET" <<<"$1" ;;
  *) exit 64 ;;
esac
