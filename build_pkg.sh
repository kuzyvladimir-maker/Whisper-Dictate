#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

ARCH="${ARCH:-$(uname -m)}"
VERSION="${WHISPER_DICTATE_VERSION:-1.0.0}"
IDENTIFIER="com.whisperdictate.installer"

case "$ARCH" in
  arm64)
    BUNDLE_SRC="$APP_DIR/dist/Whisper Dictate.app"
    OUT_DIR="$APP_DIR/dist"
    ;;
  x86_64)
    BUNDLE_SRC="$APP_DIR/dist-x86/Whisper Dictate.app"
    OUT_DIR="$APP_DIR/dist-x86"
    ;;
  *)
    echo "Unsupported ARCH: $ARCH"
    exit 1
    ;;
esac

PKG_OUT="$OUT_DIR/WhisperDictate-${VERSION}-${ARCH}.pkg"
STAGING="$OUT_DIR/pkg-staging"
SCRIPTS="$OUT_DIR/pkg-scripts"

ARCH="$ARCH" bash "$APP_DIR/build_app.sh"

echo "Staging package payload for $ARCH..."
rm -rf "$STAGING" "$SCRIPTS"
mkdir -p "$STAGING/Applications" "$SCRIPTS"
cp -R "$BUNDLE_SRC" "$STAGING/Applications/"

cat > "$SCRIPTS/postinstall" <<'POSTINSTALL'
#!/bin/bash
set -e

CONSOLE_USER=$(stat -f %Su /dev/console)
USER_HOME=$(dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory | awk '{print $2}')
USER_UID=$(id -u "$CONSOLE_USER")
APP_BUNDLE="/Applications/Whisper Dictate.app"
RESOURCES="$APP_BUNDLE/Contents/Resources"
LAUNCH_AGENT="$USER_HOME/Library/LaunchAgents/com.whisperdictate.app.plist"
KARABINER_RULES_DIR="$USER_HOME/.config/karabiner/assets/complex_modifications"
DATA_DIR="$USER_HOME/Library/Application Support/Whisper Dictate"

run_as_user() {
  /usr/bin/sudo -u "$CONSOLE_USER" "$@"
}

# Re-sign the bundle so cdhash is consistent on this machine.
/usr/bin/codesign --force --deep --sign - "$APP_BUNDLE" >/dev/null 2>&1 || true

mkdir -p "$DATA_DIR/recordings"
chown -R "$CONSOLE_USER:staff" "$DATA_DIR"

mkdir -p "$(dirname "$LAUNCH_AGENT")"
cat > "$LAUNCH_AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.whisperdictate.app</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP_BUNDLE/Contents/MacOS/Whisper Dictate</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <false/>
  <key>StandardErrorPath</key>
  <string>$DATA_DIR/whisper-dictate.err.log</string>
  <key>StandardOutPath</key>
  <string>$DATA_DIR/whisper-dictate.out.log</string>
</dict>
</plist>
PLIST
chown "$CONSOLE_USER:staff" "$LAUNCH_AGENT"

if [ -d "/Applications/Karabiner-Elements.app" ]; then
  mkdir -p "$KARABINER_RULES_DIR"
  /usr/bin/sed "s|__CONTROL_SCRIPT__|$RESOURCES/control.sh|g" \
    "$RESOURCES/whisper_fn_to_f18.template.json" \
    > "$KARABINER_RULES_DIR/whisper_fn_to_f18.json"
  chown -R "$CONSOLE_USER:staff" "$USER_HOME/.config/karabiner"
fi

run_as_user /bin/launchctl bootout "gui/$USER_UID/com.whisperdictate.app" >/dev/null 2>&1 || true
run_as_user /bin/launchctl bootstrap "gui/$USER_UID" "$LAUNCH_AGENT" >/dev/null 2>&1 || true

run_as_user /usr/bin/open "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone" >/dev/null 2>&1 || true
sleep 1
run_as_user /usr/bin/open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" >/dev/null 2>&1 || true

exit 0
POSTINSTALL

chmod +x "$SCRIPTS/postinstall"

echo "Building $PKG_OUT..."
/usr/bin/pkgbuild \
  --root "$STAGING" \
  --identifier "$IDENTIFIER" \
  --version "$VERSION" \
  --install-location "/" \
  --scripts "$SCRIPTS" \
  "$PKG_OUT"

rm -rf "$STAGING" "$SCRIPTS"

echo ""
echo "Built: $PKG_OUT"
echo "Size: $(du -h "$PKG_OUT" | cut -f1)"
