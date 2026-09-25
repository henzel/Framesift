# Third-party components

Framesift itself is MIT licensed (see `LICENSE`). The desktop bundles and the Docker image
redistribute the components below. LGPL components are included as separate shared libraries
or separate executables so that they can be replaced; ffmpeg, ffprobe and exiftool are invoked
as separate processes. License texts are shipped next to the binaries in the bundles.

| Component | Version | License | How it is used |
|---|---|---|---|
| Python | 3.12 | PSF-2.0 | runtime |
| Pillow | 12.x | MIT-CMU | image decoding, EXIF parsing |
| pi-heif (libheif 1.18.1, libde265 1.0.15) | 1.4.0 | BSD-3-Clause; bundled libraries LGPL-3.0 | HEIC decoding (dynamic libraries inside the wheel) |
| NumPy | 2.x | BSD-3-Clause | perceptual hash, sharpness, brightness |
| blake3 | 1.x | CC0-1.0 OR Apache-2.0 | content hashes |
| typer, click, rich | current | MIT, BSD-3-Clause, MIT | command line |
| psutil | 7.x | BSD-3-Clause | volume types, process priority |
| send2trash | 2.x | BSD-3-Clause | system trash for purge |
| platformdirs | 4.x | MIT | cache and config directories |
| PySide6 / Qt 6 (incl. Qt Multimedia's FFmpeg) | 6.11 | LGPL-3.0 | GUI, video playback; dynamic libraries |
| pyobjc-framework-Cocoa (macOS) | 11.x | MIT | trash API |
| FFmpeg (ffmpeg, ffprobe) | BtbN master-latest LGPL builds (Windows, Linux); 7.1.1 built from source (macOS) | LGPL-2.1-or-later (built without `--enable-gpl`/`--enable-nonfree`) | video thumbnails and frames, HEIC fallback, metadata fallback; separate process |
| ExifTool | resolved at build time from exiftool.org | Perl Artistic License 1.0 / GPL-1.0-or-later (dual) | RAW previews and metadata fallback; separate process |
| PyInstaller | 6.x | GPL-2.0-or-later with the bootloader exception | build tool only; the exception permits bundling MIT applications |

Not included on purpose: `pillow-heif` wheels (they bundle the GPL-2.0 x265 encoder),
`imagehash` (SciPy dependency; the algorithm is reimplemented), `PyAV` and `imageio-ffmpeg`
(their bundled FFmpeg builds are GPL).

HEVC and HEIF are patent-encumbered formats; the decoders used here are the open-source
libde265 and FFmpeg implementations, and no patent licenses are conveyed by this project.
