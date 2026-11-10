#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
WHISPER_DIR="$APP_DIR/vendor/whisper.cpp"
WHISPER_COMMIT="${WHISPER_CPP_COMMIT:-3e9b7d0fef3528ee2208da3cdb873a2c53d2ae2f}"
MODEL_NAME="${WHISPER_MODEL:-large-v3-turbo}"
MODEL_FILE="$WHISPER_DIR/models/ggml-$MODEL_NAME.bin"
DEFAULT_MODEL_SHA256="1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"
VAD_SHA256="2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987"
PLIST_PATH="$HOME/Library/LaunchAgents/com.whisperdictate.app.plist"
KARABINER_RULES_DIR="$HOME/.config/karabiner/assets/complex_modifications"
DATA_DIR="$HOME/Library/Application Support/Whisper Dictate"
BUNDLE="$APP_DIR/dist/Whisper Dictate.app"
ARCH="$(uname -m)"

info() { printf '%s\n' "$1"; }

need_brew() {
  if ! command -v brew >/dev/null 2>&1; then
    info "Homebrew is required. Install it from https://brew.sh, then run this script again."
    exit 1
  fi
}

ensure_brew_package() {
  local name="$1"
  if ! brew list "$name" >/dev/null 2>&1; then
    brew install "$name"
  fi
}

ensure_python() {
  need_brew
  if ! command -v python3.11 >/dev/null 2>&1; then
    ensure_brew_package python@3.11
  fi
}

install_python_deps() {
  python3.11 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip wheel
  "$VENV_DIR/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
}

build_whisper_cpp() {
  need_brew
  ensure_brew_package git
  ensure_brew_package cmake

  if [ ! -d "$WHISPER_DIR/.git" ]; then
    rm -rf "$WHISPER_DIR"
    git clone --no-checkout https://github.com/ggml-org/whisper.cpp.git "$WHISPER_DIR"
  fi
  git -C "$WHISPER_DIR" fetch --depth 1 origin "$WHISPER_COMMIT"
  git -C "$WHISPER_DIR" checkout --detach FETCH_HEAD

  local cmake_args=(-S "$WHISPER_DIR" -B "$WHISPER_DIR/build" -DCMAKE_BUILD_TYPE=Release)
  if [ "$ARCH" = "arm64" ]; then
    cmake_args+=(-DGGML_METAL=ON)
  fi
  cmake "${cmake_args[@]}"
  cmake --build "$WHISPER_DIR/build" --config Release -j"$(sysctl -n hw.ncpu)"
}

download_model() {
  mkdir -p "$WHISPER_DIR/models"
  if [ ! -f "$MODEL_FILE" ]; then
    info "Downloading Whisper model: $MODEL_NAME"
    if [ -x "$WHISPER_DIR/models/download-ggml-model.sh" ]; then
      (cd "$WHISPER_DIR" && ./models/download-ggml-model.sh "$MODEL_NAME")
    else
      curl -L "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$MODEL_NAME.bin" -o "$MODEL_FILE"
    fi
  fi
  local expected="${WHISPER_MODEL_SHA256:-}"
  if [ "$MODEL_NAME" = "large-v3-turbo" ] && [ -z "$expected" ]; then
    expected="$DEFAULT_MODEL_SHA256"
  fi
  verify_sha256 "$MODEL_FILE" "$expected"
}

download_vad_model() {
  local vad_file="$WHISPER_DIR/models/ggml-silero-v6.2.0.bin"
  if [ ! -f "$vad_file" ]; then
    "$WHISPER_DIR/models/download-vad-model.sh" silero-v6.2.0
  fi
  verify_sha256 "$vad_file" "$VAD_SHA256"
}

verify_sha256() {
  local path="$1"
  local expected="$2"
  if [ -z "$expected" ]; then
    info "Warning: no SHA-256 configured for $path"
    return
  fi
  local actual
  actual=$(shasum -a 256 "$path" | awk '{print $1}')
  if [ "$actual" != "$expected" ]; then
    info "SHA-256 mismatch for $path"
    exit 1
  fi
}

install_karabiner_rule() {
  if [ ! -d "/Applications/Karabiner-Elements.app" ]; then
    brew install --cask karabiner-elements
  fi
  mkdir -p "$KARABINER_RULES_DIR"
  sed "s|__CONTROL_SCRIPT__|$APP_DIR/control.sh|g" \
    "$APP_DIR/karabiner/whisper_fn_to_f18.template.json" \
    > "$KARABINER_RULES_DIR/whisper_fn_to_f18.json"
}

install_launch_agent() {
  mkdir -p "$DATA_DIR/recordings"
  chmod 700 "$DATA_DIR" "$DATA_DIR/recordings"
  touch "$APP_DIR/whisper-dictate.err.log" "$APP_DIR/whisper-dictate.out.log"
  chmod 600 "$APP_DIR/whisper-dictate.err.log" "$APP_DIR/whisper-dictate.out.log"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.whisperdictate.app</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BUNDLE/Contents/MacOS/Whisper Dictate</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>StandardErrorPath</key>
  <string>$APP_DIR/whisper-dictate.err.log</string>
  <key>StandardOutPath</key>
  <string>$APP_DIR/whisper-dictate.out.log</string>
</dict>
</plist>
PLIST
  launchctl bootout "gui/$(id -u)/com.whisperdictate.app" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
}

main() {
  info "Installing Whisper Dictate (dev mode)..."
  ensure_python
  install_python_deps
  build_whisper_cpp
  download_model
  download_vad_model
  install_karabiner_rule
  bash "$APP_DIR/build_app.sh"
  install_launch_agent
  info ""
  info "Done. Final manual steps macOS requires:"
  info "  1. Karabiner-Elements > Complex Modifications > Add rule > enable 'Whisper Dictate: Fn/Globe to F18'."
  info "  2. System Settings > Privacy & Security:"
  info "     - Microphone: allow Whisper Dictate."
  info "     - Accessibility: add and enable $BUNDLE"
  info "  3. Hold Fn in any text field, speak, release. Or double-tap Fn for toggle mode."
  info ""
  info "To package an installer for friends: bash build_pkg.sh"
}

main "$@"
