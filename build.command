#!/bin/bash
# Builds "NowPlaying Bridge.app" into the dist/ folder.
# Run this on a Mac: double-click it, or `bash build.command` in Terminal.

cd "$(dirname "$0")" || exit 1

echo
echo "=== NowPlaying Bridge for macOS - build ==="
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 was not found."
  echo "Install it from python.org (the full installer, not just the App Store),"
  echo "then run this again."
  read -r -p "Press Return to close."
  exit 1
fi

echo "Installing PyInstaller..."
python3 -m pip install --upgrade pip
python3 -m pip install pyinstaller || {
  echo
  echo "Install failed. Scroll up for the reason."
  read -r -p "Press Return to close."
  exit 1
}

echo
echo "Building the app..."
python3 -m PyInstaller --windowed --noconfirm \
  --name "NowPlaying Bridge" \
  --icon nowplaying-bridge.icns \
  --osx-bundle-identifier com.federicoramirezhonack.nowplayingbridge \
  --hidden-import status_window \
  --add-data "status_window.py:." \
  nowplaying_bridge_mac.py || {
  echo
  echo "Build failed. Scroll up for the reason."
  read -r -p "Press Return to close."
  exit 1
}

PLIST="dist/NowPlaying Bridge.app/Contents/Info.plist"

# Without a usage description macOS terminates the app instead of asking for
# Automation permission. PyInstaller does not add one.
/usr/libexec/PlistBuddy -c 'Add :NSAppleEventsUsageDescription string "NowPlaying Bridge reads the track playing in Music and Spotify so your stream overlay can show it."' "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c 'Set :NSAppleEventsUsageDescription "NowPlaying Bridge reads the track playing in Music and Spotify so your stream overlay can show it."' "$PLIST"

# Ad-hoc signing gives the app a stable identity, so a granted Automation
# permission survives rebuilds. It does not remove the Gatekeeper warning.
codesign --force --deep --sign - "dist/NowPlaying Bridge.app" && \
  codesign --verify --verbose "dist/NowPlaying Bridge.app"

echo
echo "Done. Your app is at:"
echo "  dist/NowPlaying Bridge.app"
echo
echo "Double-click it to test. To share it, zip it properly with:"
echo "  cd dist && ditto -c -k --sequesterRsrc --keepParent \"NowPlaying Bridge.app\" NowPlayingBridge-mac.zip"
echo
read -r -p "Press Return to close."
