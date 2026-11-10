#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

PYTHON="${PYTHON:-$APP_DIR/.venv/bin/python}"

"$PYTHON" -m py_compile main.py paste.py recorder.py transcriber.py vocabulary.py whisper_server.py cleaner.py gemini_client.py live_chunker.py
"$PYTHON" -m ruff check main.py paste.py recorder.py transcriber.py vocabulary.py whisper_server.py cleaner.py gemini_client.py live_chunker.py tests
"$PYTHON" -m unittest discover -s tests -v
bash -n install.sh build_app.sh build_pkg.sh uninstall.sh control.sh check.sh
jq empty config.json karabiner/whisper_fn_to_f18.template.json

echo "All checks passed."
