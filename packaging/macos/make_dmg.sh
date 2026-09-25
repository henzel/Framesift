#!/usr/bin/env bash
# Package dist/Framesift.app into a DMG; sign and notarize when Apple secrets are present.
# Usage: packaging/macos/make_dmg.sh <arch-label> (arm64 | x86_64)
set -euo pipefail

ARCH="${1:-$(uname -m)}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP="$ROOT/dist/Framesift.app"
VERSION="$(python -c 'import framesift; print(framesift.__version__)')"
OUT_DIR="$ROOT/packaging/out"
DMG="$OUT_DIR/Framesift-$VERSION-macos-$ARCH.dmg"
mkdir -p "$OUT_DIR"

if [[ ! -d "$APP" ]]; then
  echo "missing $APP (run pyinstaller first)" >&2
  exit 1
fi

if [[ -n "${APPLE_CERTIFICATE_P12:-}" && -n "${APPLE_CERTIFICATE_PASSWORD:-}" && -n "${APPLE_TEAM_ID:-}" ]]; then
  echo "Signing with Developer ID"
  KEYCHAIN="$RUNNER_TEMP/framesift.keychain-db"
  echo "$APPLE_CERTIFICATE_P12" | base64 --decode > "$RUNNER_TEMP/cert.p12"
  security create-keychain -p "" "$KEYCHAIN"
  security set-keychain-settings -lut 21600 "$KEYCHAIN"
  security unlock-keychain -p "" "$KEYCHAIN"
  security import "$RUNNER_TEMP/cert.p12" -P "$APPLE_CERTIFICATE_PASSWORD" -A -t cert -f pkcs12 -k "$KEYCHAIN"
  security list-keychain -d user -s "$KEYCHAIN"
  IDENTITY="$(security find-identity -v -p codesigning "$KEYCHAIN" | grep -o '"Developer ID Application: [^"]*"' | head -1 | tr -d '"')"
  # sign nested binaries first (ffmpeg, exiftool perl script is data), then the bundle
  find "$APP/Contents" -type f \( -perm -u+x -o -name "*.dylib" -o -name "*.so" \) -print0 \
    | xargs -0 -n1 codesign --force --options runtime --timestamp --sign "$IDENTITY" || true
  codesign --force --deep --options runtime --timestamp --entitlements "$ROOT/packaging/macos/entitlements.plist" --sign "$IDENTITY" "$APP"
  codesign --verify --deep --strict "$APP"
  SIGNED=1
else
  echo "No Apple signing secrets: producing an unsigned build (see README for how to open it)"
  SIGNED=0
fi

rm -f "$DMG"
STAGING="$OUT_DIR/dmg-staging"
rm -rf "$STAGING" && mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
hdiutil create -volname "Framesift $VERSION" -srcfolder "$STAGING" -ov -format UDZO "$DMG"
rm -rf "$STAGING"

if [[ "$SIGNED" == "1" && -n "${APPLE_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" ]]; then
  echo "Notarizing"
  codesign --force --timestamp --sign "$IDENTITY" "$DMG"
  xcrun notarytool submit "$DMG" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD" --wait
  xcrun stapler staple "$DMG"
fi
echo "created $DMG"
