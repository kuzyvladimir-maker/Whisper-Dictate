#!/bin/bash
# Sends "press" or "release" to the Whisper Dictate control socket.
# Invoked by Karabiner-Elements on Fn key events.
exec /usr/bin/nc -U -w 1 /tmp/whisper-dictate.sock <<<"$1"
