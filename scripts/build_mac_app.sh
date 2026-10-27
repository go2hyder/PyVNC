#!/bin/bash
# Build "dist/PyVNC Server.app" (menu bar app). Add --dmg to also create dist/PyVNC-Server.dmg.
# Usage: scripts/build_mac_app.sh [--dmg]
set -euo pipefail

cd "$(dirname "$0")/.."
APP_NAME="PyVNC Server"
APP="dist/$APP_NAME.app"

if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements-server.txt pyinstaller
[[ -f assets/PyVNC.icns ]] || .venv/bin/python scripts/make_icons.py

.venv/bin/pyinstaller --noconfirm --clean --log-level WARN \
    --windowed \
    --name "$APP_NAME" \
    --icon "$PWD/assets/PyVNC.icns" \
    --osx-bundle-identifier com.pyvnc.server \
    --specpath build \
    --paths "$PWD" \
    --hidden-import keyring.backends.macOS \
    --collect-submodules server \
    --collect-submodules common \
    "$PWD/run_server_app.py"

PLIST="$APP/Contents/Info.plist"
# Menu bar only: no Dock icon, no app menu.
plutil -replace LSUIElement -bool true "$PLIST"
plutil -replace CFBundleDisplayName -string "$APP_NAME" "$PLIST"
plutil -replace CFBundleShortVersionString -string "1.0.0" "$PLIST"
plutil -replace NSHumanReadableCopyright -string "PyVNC" "$PLIST"

# Ad-hoc signature gives macOS a stable identity for Screen Recording / Accessibility grants.
codesign --force --deep --sign - "$APP"
codesign --verify --deep "$APP"
echo "Built $APP"

if [[ "${1:-}" == "--dmg" ]]; then
    STAGING="build/dmg"
    rm -rf "$STAGING" && mkdir -p "$STAGING"
    cp -R "$APP" "$STAGING/"
    ln -s /Applications "$STAGING/Applications"
    hdiutil create -volname "$APP_NAME" -srcfolder "$STAGING" -ov -format UDZO "dist/PyVNC-Server.dmg" >/dev/null
    echo "Built dist/PyVNC-Server.dmg"
fi

echo
echo "Next: drag the app into /Applications, open it (right-click > Open the first time),"
echo "then grant Screen Recording and Accessibility to \"$APP_NAME\" in System Settings."
