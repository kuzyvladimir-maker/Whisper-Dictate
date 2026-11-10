#!/usr/bin/env bash
# Builds "Whisper Dictate Lite.app" from Sources/ with plain swiftc (Xcode Command Line Tools
# are enough, no Xcode project).
#   bash build.sh [output.app]
#   ARCHS="x86_64 arm64" bash build.sh     # universal binary
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/build/Whisper Dictate Lite.app}"
ARCHS="${ARCHS:-$(uname -m)}"
MIN_MACOS="${MIN_MACOS:-11.0}"
VERSION="0.1.0"
BUNDLE_ID="com.whisperdictate.lite"
EXE="WhisperDictateLite"

if ! xcrun --find swiftc >/dev/null 2>&1; then
  echo "swiftc не найден. Установите Command Line Tools: xcode-select --install" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

bins=()
for arch in $ARCHS; do
  echo "Компиляция $arch (macOS $MIN_MACOS+)..."
  xcrun swiftc -O -swift-version 5 \
    -target "$arch-apple-macos$MIN_MACOS" \
    -module-name WhisperDictateLite \
    -o "$WORK/$EXE-$arch" \
    "$HERE"/Sources/*.swift
  bins+=("$WORK/$EXE-$arch")
done

rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
if [ "${#bins[@]}" -gt 1 ]; then
  lipo -create "${bins[@]}" -output "$OUT/Contents/MacOS/$EXE"
else
  cp "${bins[0]}" "$OUT/Contents/MacOS/$EXE"
fi
cp "$HERE/dictionary.example.txt" "$OUT/Contents/Resources/"

cat > "$OUT/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleName</key><string>Whisper Dictate Lite</string>
  <key>CFBundleDisplayName</key><string>Whisper Dictate Lite</string>
  <key>CFBundleExecutable</key><string>$EXE</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>$MIN_MACOS</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Запись голоса для диктовки. Аудио не сохраняется.</string>
</dict>
</plist>
PLIST

codesign --force --sign - --identifier "$BUNDLE_ID" "$OUT" >/dev/null
echo "Готово: $OUT ($(lipo -archs "$OUT/Contents/MacOS/$EXE"))"
