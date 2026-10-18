#!/bin/sh
# Claude Code hook (SessionStart / UserPromptSubmit): new answers from Jackie for repo Whisper-Dictate. Silent when empty or offline.
out=$(ssh -o BatchMode=yes -o ConnectTimeout=4 server 'python3 /root/.openclaw/workspace/scripts/repo_jackie.py Whisper-Dictate inbox' 2>/dev/null)
[ -n "$out" ] && printf 'Новые сообщения от Джеки (ящик Whisper-Dictate):\n%s\n' "$out"
exit 0
