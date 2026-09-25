#!/usr/bin/env bash
# Build an LGPL-only static ffmpeg/ffprobe for macOS (no --enable-gpl, no --enable-nonfree).
# Output: packaging/bin/macos/{ffmpeg,ffprobe,LICENSE-ffmpeg.txt}
# Usage: packaging/macos/build_ffmpeg.sh [version]   (default 7.1.1)
set -euo pipefail

VERSION="${1:-7.1.1}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/packaging/bin/macos"
WORK="${FFMPEG_BUILD_DIR:-$ROOT/packaging/out/ffmpeg-src}"
mkdir -p "$OUT" "$WORK"

if [[ -x "$OUT/ffmpeg" && -x "$OUT/ffprobe" ]] && "$OUT/ffmpeg" -version | head -1 | grep -q "$VERSION"; then
  echo "ffmpeg $VERSION already built in $OUT"
  exit 0
fi

cd "$WORK"
if [[ ! -d "ffmpeg-$VERSION" ]]; then
  curl -fsSL "https://ffmpeg.org/releases/ffmpeg-$VERSION.tar.xz" -o "ffmpeg-$VERSION.tar.xz"
  curl -fsSL "https://ffmpeg.org/releases/ffmpeg-$VERSION.tar.xz.asc" -o "ffmpeg-$VERSION.tar.xz.asc" || true
  tar xf "ffmpeg-$VERSION.tar.xz"
fi
cd "ffmpeg-$VERSION"

# LGPL configuration: native decoders (HEVC/H.264/AAC…), VideoToolbox hardware decode, no external GPL libs.
./configure \
  --prefix="$WORK/prefix" \
  --disable-gpl --disable-nonfree --disable-version3 \
  --disable-shared --enable-static --pkg-config-flags="--static" \
  --disable-doc --disable-htmlpages --disable-manpages --disable-podpages --disable-txtpages \
  --disable-ffplay --disable-debug \
  --enable-videotoolbox --enable-audiotoolbox \
  --enable-pthreads \
  --disable-libxcb --disable-sdl2 --disable-xlib \
  --extra-cflags="-mmacosx-version-min=12.0" --extra-ldflags="-mmacosx-version-min=12.0"
make -j"$(sysctl -n hw.ncpu)"
cp ffmpeg ffprobe "$OUT/"
cp COPYING.LGPLv2.1 "$OUT/LICENSE-ffmpeg.txt"
strip "$OUT/ffmpeg" "$OUT/ffprobe" || true
echo "built LGPL ffmpeg $VERSION → $OUT"
"$OUT/ffmpeg" -version | head -2
