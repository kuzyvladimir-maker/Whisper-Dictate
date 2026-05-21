#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

ARCH="${ARCH:-$(uname -m)}"
case "$ARCH" in
  arm64)
    VENV=".venv"
    WHISPER_BUILD="vendor/whisper.cpp/build"
    OUT_BUNDLE="dist/Whisper Dictate.app"
    ARCH_PREFIX=""
    ;;
  x86_64)
    VENV=".venv-x86"
    WHISPER_BUILD="vendor/whisper.cpp/build-x86"
    OUT_BUNDLE="dist-x86/Whisper Dictate.app"
    ARCH_PREFIX="arch -x86_64"
    ;;
  *)
    echo "Unsupported ARCH: $ARCH (expected arm64 or x86_64)"
    exit 1
    ;;
esac

BUNDLE_PARENT="$(dirname "$OUT_BUNDLE")"
RESOURCES="$OUT_BUNDLE/Contents/Resources"
WHISPER_BIN="$WHISPER_BUILD/bin/whisper-cli"

if [ ! -x "$WHISPER_BIN" ]; then
  echo "whisper-cli not found at $WHISPER_BIN. Run install.sh (or the x86 setup) first."
  exit 1
fi

echo "Building Whisper Dictate.app for $ARCH..."
$ARCH_PREFIX "$VENV/bin/python" -m pip install --upgrade pyinstaller >/dev/null
rm -rf "build-$ARCH" "$BUNDLE_PARENT"

$ARCH_PREFIX "$VENV/bin/pyinstaller" \
  --noconfirm \
  --windowed \
  --name "Whisper Dictate" \
  --osx-bundle-identifier "com.whisperdictate.app" \
  --distpath "$BUNDLE_PARENT" \
  --workpath "build-$ARCH" \
  --target-arch "$ARCH" \
  main.py

BIN_DIR="$RESOURCES/whisper-cli-bin"
WHISPER_SERVER="$WHISPER_BUILD/bin/whisper-server"
mkdir -p "$BIN_DIR"
cp "$WHISPER_BIN" "$BIN_DIR/"
if [ -x "$WHISPER_SERVER" ]; then
  cp "$WHISPER_SERVER" "$BIN_DIR/"
fi
find "$WHISPER_BUILD" -name "*.dylib" -exec cp {} "$BIN_DIR/" \;

for f in "$BIN_DIR/whisper-cli" "$BIN_DIR/whisper-server" "$BIN_DIR"/*.dylib; do
  [ -f "$f" ] || continue
  while IFS= read -r rpath; do
    install_name_tool -delete_rpath "$rpath" "$f" 2>/dev/null || true
  done < <(otool -l "$f" | awk '/LC_RPATH/{flag=1;next} flag && /path /{print $2; flag=0}')
  install_name_tool -add_rpath "@loader_path" "$f" 2>/dev/null || true
done

cp config.json "$RESOURCES/"
cp control.sh "$RESOURCES/control.sh"
chmod +x "$RESOURCES/control.sh"
cp karabiner/whisper_fn_to_f18.template.json "$RESOURCES/"

MODEL_FILE="$(jq -r .model_filename config.json)"
MODEL_SRC="vendor/whisper.cpp/models/$MODEL_FILE"
if [ -f "$MODEL_SRC" ]; then
  mkdir -p "$RESOURCES/models"
  cp "$MODEL_SRC" "$RESOURCES/models/$MODEL_FILE"
else
  echo "Warning: model not found at $MODEL_SRC. Bundle will not include it."
fi

PLIST="$OUT_BUNDLE/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string Whisper Dictate records your voice locally for transcription." "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSAppleEventsUsageDescription string Whisper Dictate pastes transcribed text into the active app." "$PLIST" 2>/dev/null || true

codesign --force --deep --sign - "$OUT_BUNDLE"

echo "Built: $APP_DIR/$OUT_BUNDLE"
