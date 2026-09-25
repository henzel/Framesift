# Changelog

All notable changes to Framesift are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-09-25

First release.

### Added
- Folder browser for Source, Review and Delete with live counts, virtualized thumbnail grid
  (100k+ items), sort by date/name/size, photo/video filter, multi-selection actions.
- Manual review mode: keep / delete / skip / multi-level undo, video playback (Qt Multimedia
  with an ffmpeg frame fallback), Live Photo playback, zoom and pan, info panel with the
  automatic category and reason, session counters.
- Group mode for duplicate and similar-shot groups with a pre-marked best frame.
- Automatic mode: scan → classify → dry-run report → apply, with adjustable thresholds that
  re-evaluate without rescanning, pause/resume/cancel, and the "move all Live Photo videos"
  action.
- Eight categories: duplicates, screenshots, screen recordings, short videos, media without
  camera data, junk, blurry/dark, similar; report-only Live Photo videos, edited pairs and
  largest videos.
- Journal of every move with undo by last action, session, category or everything, also after
  restart and from another machine.
- Purge (the only deleting operation) to the system trash on local disks, with an explicit
  permanent-delete warning on network volumes.
- Header-only metadata readers for JPEG, PNG, WebP, GIF, TIFF/DNG, HEIC/HEIF and MOV/MP4/3GP
  (Apple MakerNote ContentIdentifier, QuickTime keys); exiftool/ffprobe as optional fallbacks.
- SQLite catalog shared between the desktop app and the CLI (for example on a NAS over SMB),
  with a heartbeat lock and per-host folder mapping.
- `framesift` CLI (`scan`, `classify`, `report`, `apply`, `undo`, `purge`, `status`) with
  dry-run defaults, JSON/CSV reports and tail-friendly logs; Docker image for
  linux/amd64 and linux/arm64.
- English and Russian interface, light and dark themes following the system.
- Desktop bundles for macOS (Apple Silicon, Intel), Windows x64 and Linux x64 (AppImage) with
  LGPL builds of ffmpeg and exiftool included.

[Unreleased]: https://github.com/henzel/PhotoSift/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/henzel/PhotoSift/releases/tag/v0.1.0
