#!/usr/bin/env bash
# Wrap dist/Framesift (PyInstaller one-dir) into an AppImage.
# Usage: packaging/linux/make_appimage.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIST="$ROOT/dist/Framesift"
VERSION="$(python -c 'import framesift; print(framesift.__version__)')"
ARCH="$(uname -m)"
OUT_DIR="$ROOT/packaging/out"
APPDIR="$OUT_DIR/Framesift.AppDir"
TOOL="$OUT_DIR/appimagetool"
mkdir -p "$OUT_DIR"

if [[ ! -d "$DIST" ]]; then
  echo "missing $DIST (run pyinstaller first)" >&2
  exit 1
fi
if [[ ! -x "$TOOL" ]]; then
  curl -fsSL "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage" -o "$TOOL"
  chmod +x "$TOOL"
fi

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -R "$DIST"/. "$APPDIR/usr/bin/"
cat > "$APPDIR/AppRun" <<'EOF'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "$0")")"
export PATH="$HERE/usr/bin/bin:$HERE/usr/bin:$PATH"
if [[ "${1:-}" == "cli" ]]; then
  shift
  exec "$HERE/usr/bin/framesift" "$@"
fi
exec "$HERE/usr/bin/framesift-gui" "$@"
EOF
chmod +x "$APPDIR/AppRun"
cat > "$APPDIR/framesift.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Framesift
Comment=Sort large photo and video archives safely
Exec=framesift-gui %f
Icon=framesift
Categories=Graphics;Photography;
Terminal=false
EOF
cp "$APPDIR/framesift.desktop" "$APPDIR/usr/share/applications/"
ICON="$ROOT/framesift/resources/framesift.png"
if [[ -f "$ICON" ]]; then
  cp "$ICON" "$APPDIR/framesift.png"
  cp "$ICON" "$APPDIR/usr/share/icons/hicolor/256x256/apps/framesift.png"
else
  python - "$APPDIR/framesift.png" <<'EOF'
import sys
from PIL import Image, ImageDraw
im = Image.new("RGBA", (256, 256), (30, 90, 160, 255))
d = ImageDraw.Draw(im)
d.rectangle([40, 70, 216, 200], outline=(255, 255, 255, 255), width=12)
d.ellipse([100, 105, 156, 161], fill=(255, 200, 60, 255))
im.save(sys.argv[1])
EOF
  cp "$APPDIR/framesift.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/framesift.png"
fi

APPIMAGE="$OUT_DIR/Framesift-$VERSION-linux-$ARCH.AppImage"
ARCH="$ARCH" "$TOOL" --no-appstream "$APPDIR" "$APPIMAGE"
echo "created $APPIMAGE"
