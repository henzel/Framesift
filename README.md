# Framesift

**Sort a huge photo and video archive without losing anything.**
Framesift is a free, open-source desktop app (macOS, Windows, Linux) plus a command-line tool
for the hundreds of gigabytes of old phone exports that nobody dares to touch. It never deletes
or modifies a file on its own: every action is a move, every move is journaled, and every move
can be undone.

[Русская версия](README.ru.md) · [Architecture](ARCHITECTURE.md) · [Changelog](CHANGELOG.md)

![Folder browser](docs/screenshots/browser.png)

## What it does

You point Framesift at three folders:

| Folder | Purpose |
|---|---|
| **Source** | where the photos and videos live (subfolders optional) |
| **Review** | where the automatic mode stages junk candidates, sorted by category |
| **To delete** | where everything you decided to delete waits until you empty it |

and work in two modes:

* **Manual** — one item at a time: `→` keep, `←` to delete, `↓` decide later, `Ctrl/Cmd+Z` undo.
* **Automatic** — local analysis finds duplicates, screenshots, screen recordings, short videos,
  media without camera data, junk files, blurry or dark frames and bursts of similar shots, shows
  you a dry-run report, and moves only the categories you confirm into the Review folder. You
  then look through them manually, or by group for duplicates and similar shots.

Plus a folder browser for the three folders, a journal of every move, and one explicit
"Empty the Delete folder" button that is the only thing that ever deletes anything.

Framesift understands iPhone archives: HEIC, Live Photos (photo + MOV stay together), `.AAE`
edit sidecars, edited copies (`IMG_E1234`), HEVC video, and the quirks of Cyrillic and
NFD/NFC file names on macOS and SMB shares.

## Safety rules

* File contents are never modified and metadata is never written.
* Nothing is ever overwritten: a name clash gets a `~2` suffix and a journal note.
* Moves on the same volume are renames (instant, attributes preserved). Across volumes they are
  copy → hash check → only then remove the original.
* Every move (manual or automatic) is recorded in the catalog with where-from, where-to, when, why
  and by which session. Undo the last action, a whole session, a category, or everything, also
  after a restart and also from another machine.
* Service folders (`@eaDir`, `#recycle`, `.Trashes`, `$RECYCLE.BIN`, …) and the Review and Delete
  folders are never scanned. Apple Photos libraries are refused; Lightroom catalogs get a warning.
* Only **Empty the Delete folder** deletes files: to the system trash on local disks, and with an
  explicit "this is permanent" warning on network volumes (a NAS recycle bin or snapshots may
  still keep the data).

## Install

Downloads are on the [Releases](https://github.com/henzel/framesift/releases) page.

* **macOS** (Apple Silicon and Intel builds): open the `.dmg` and drag Framesift to Applications.
  The build is unsigned unless the maintainer has an Apple Developer certificate configured.
  On first start macOS says it cannot verify the app: click **Done**, then open
  **System Settings → Privacy & Security**, scroll down and click **Open Anyway** next to the
  Framesift message. Or run `xattr -dr com.apple.quarantine /Applications/Framesift.app` once.
  (Right-click → **Open** no longer works for unsigned apps since macOS 15.)
* **Windows 10/11 x64**: unzip and run `framesift-gui.exe`. SmartScreen will say "Windows protected
  your PC" for an unsigned build: click **More info** → **Run anyway**. The command-line tool is
  `framesift.exe` in the same folder.
* **Linux x64**: `chmod +x Framesift-*.AppImage && ./Framesift-*.AppImage`. The CLI is
  `./Framesift-*.AppImage cli …`.
* **From source** (any OS with Python 3.12): `pip install "framesift[gui] @ git+https://github.com/henzel/framesift"`,
  then `framesift-gui`. Install `ffmpeg` (video thumbnails and playback fallback) and optionally
  `exiftool` (RAW previews) with your package manager; the desktop bundles ship LGPL builds.

## Quick start

1. **Settings** → choose the Source folder. Review and To-delete default to
   `<Source>/_framesift_review` and `<Source>/_framesift_delete` (both are excluded from scanning).
   Framesift warns if the three folders are on different volumes.
2. **Open** — the folder is scanned in the background; the browser fills up as it goes.
3. **Automatic** → **Run analysis** → look at the report → untick what you disagree with, move the
   sliders if needed → **Apply selected categories**. Candidates land in
   `<Review>/<category>/<original relative path>`; similar shots in `<Review>/similar/<group>/`.
4. **Review** the staged items (double-click anything in the browser, or use **Groups** for
   duplicates and similar shots). `→` returns an item to its original place, `←` sends it to
   To-delete.
5. When you are done: **Settings** → **Empty the Delete folder…**. Until then everything can be
   undone from the **Journal** page.

## Keyboard

Manual mode:

| Key | Action |
|---|---|
| `→` | keep (Source: mark reviewed · Review/Delete: return to the original place) |
| `←` | move to To-delete (nothing happens if it is already there) |
| `↓` | skip, decide later |
| `Ctrl+Z` / `⌘Z` | undo the last action (as many levels as you like) |
| `Space` / `M` | play–pause / mute video |
| `Z` | 100 % ↔ fit to window; drag to pan |
| `L` (hold) | play the Live Photo video |
| `I` | show or hide the info panel |
| `Esc` | back to the browser |

Group mode: `←`/`→` select a frame, `K` toggle "keep", `Enter` apply (marked frames stay, the
rest go to To-delete), `↓` skip the group, `Ctrl/Cmd+Z` undo. The best frame is pre-marked.

Browser: click, `Shift`, `Ctrl/Cmd` and rubber-band selection; **Keep**, **Delete**, **Restore**
act on the selection; double-click or `Enter` opens manual mode at that item with the current
sort and filter.

## The automatic categories

| Category | Confidence | Rule |
|---|---|---|
| duplicates | high | identical content (BLAKE3). The copy with the cleanest name stays (no ` (1)`, `copy`, `-1`); ties go to the earlier date, then the shorter path |
| screenshots | high | EXIF UserComment `Screenshot`, or a PNG without camera data at a phone screen resolution |
| screen_recordings | high | `RPReplay_Final*`, or a video without camera data at a phone screen resolution |
| short_videos | medium | shorter than 3 s (adjustable); Live Photo videos are never counted |
| no_camera_media | low | no camera make/model in the metadata — usually messenger downloads, sometimes photos other people sent you |
| junk | high | `.DS_Store`, `._*`, `Thumbs.db`, `desktop.ini`, empty files, files that cannot be decoded, orphaned `.AAE`; optionally all `.AAE` |
| blurry_dark | low | very low sharpness (Laplacian variance) or an almost black frame; conservative thresholds |
| similar | medium | perceptual hash within ±60 s of each other; the sharpest (then largest) frame stays |

A file that matches several categories goes to the first one in this order. Three more things
are reported without moving anything: Live Photo videos (with a separate button to stage all of
them, which turns the photos into stills), original + edited pairs, and the 100 largest videos
with a rough estimate of what re-encoding H.264 to HEVC would save.

## Running on a NAS (Docker)

The heavy work — scanning, hashing, pHash — should run where the disks are. The Docker image
contains the CLI, ffmpeg/ffprobe (LGPL) and exiftool; it works on `linux/amd64` and
`linux/arm64`. The catalog lives in `<Review>/.framesift/`, so the desktop app on your Mac or
PC can open the same folder over SMB afterwards and continue with what the NAS computed.

**Synology DSM 7 — SSH** (Control Panel → Terminal & SNMP → Enable SSH service; log in as an
administrator; Docker on DSM needs `sudo`):

```bash
# find your user id and group id once
id photo-user            # → uid=1026(photo-user) gid=100(users)

sudo docker run --rm -it --user 1026:100 \
  -v /volume1/photo:/data \
  ghcr.io/henzel/framesift:latest \
  scan --source /data/Archive --low-priority

sudo docker run --rm -it --user 1026:100 -v /volume1/photo:/data ghcr.io/henzel/framesift:latest \
  classify --catalog /data/Archive/_framesift_review --low-priority --workers 2
sudo docker run --rm -it --user 1026:100 -v /volume1/photo:/data ghcr.io/henzel/framesift:latest \
  report --catalog /data/Archive/_framesift_review
sudo docker run --rm -it --user 1026:100 -v /volume1/photo:/data ghcr.io/henzel/framesift:latest \
  apply --catalog /data/Archive/_framesift_review            # dry run
sudo docker run --rm -it --user 1026:100 -v /volume1/photo:/data ghcr.io/henzel/framesift:latest \
  apply --catalog /data/Archive/_framesift_review --yes      # really move
```

**Synology DSM 7 — Container Manager (GUI):** Container Manager cannot set the user id for a
plain container, so create a **Project** with this `docker-compose.yml` (Container Manager →
Project → Create → paste):

```yaml
services:
  framesift:
    image: ghcr.io/henzel/framesift:latest
    user: "1026:100"          # your uid:gid from `id`
    volumes:
      - /volume1/photo:/data
    command: ["scan", "--source", "/data/Archive", "--low-priority"]
```

Change `command` for each step (`classify`, `report`, `apply --yes`, `undo --all --yes`,
`purge --yes`). Progress goes to `<Review>/.framesift/logs/framesift-<date>.log` (`tail -f` it),
full lists to `<Review>/.framesift/reports/`. `--low-priority` uses `nice`/`ionice` so the NAS
stays responsive; add `--cpu-shares 256` to `docker run` for an extra brake.

## Command line

```
framesift scan     --source DIR [--catalog DIR] [--delete-dir DIR] [--no-recursive] [--workers N] [--low-priority]
framesift classify --catalog DIR [--categories a,b,…] [--short-video-seconds 3] [--similar-window 60]
                   [--similar-distance 6] [--blur-threshold 12] [--dark-threshold 0.03] [--move-all-aae]
framesift report   --catalog DIR [--json] [--out DIR]
framesift apply    --catalog DIR [--categories …] [--live-photos] [--yes]
framesift undo     --catalog DIR (--last | --session ID | --category NAME | --all) [--yes]
framesift purge    --catalog DIR [--yes] [--permanent]
framesift status   --catalog DIR [--json]
```

`apply`, `undo` and `purge` are dry runs unless you add `--yes`. Console output is short (totals
and up to five examples per category); `--json` prints machine-readable summaries, and the full
lists are always written as CSV/JSON next to the catalog. Exit codes: 0 ok, 1 error, 2 usage,
3 catalog locked, 4 refused (Apple Photos library), 5 finished with skipped files.

## FAQ

**Is anything uploaded anywhere?** No. Everything runs locally; there is no network code.

**Where is the catalog?** `<Review>/.framesift/catalog.db` (SQLite), next to the lock file,
logs and reports. Thumbnails are cached in your user cache folder (2 GB by default, adjustable),
never inside your photo folders.

**Two machines at once?** One writer at a time: the catalog has a lock file with a heartbeat.
When the NAS is running a job, the desktop app opens the catalog read-only and shows the
progress; a stale lock from a crashed process can be taken over.

**What about HEIC?** Decoded with libheif (via `pi-heif`, LGPL) and, as a fallback, with the
bundled ffmpeg. Metadata, including the Apple ContentIdentifier that pairs Live Photos, is read
directly from the file headers without decoding.

**RAW files?** DNG previews work out of the box; other RAW formats show their embedded preview
when `exiftool` is available (bundled in the desktop builds). RAW files are never classified as
"no camera data" or "junk" on the basis of missing metadata.

**Why did a similar-shot group choose that frame?** The sharpest frame (Laplacian variance on a
small preview) wins, then the larger resolution. Change it in group mode with `K`.

**Undo after I emptied the Delete folder?** Purge is the one irreversible operation. On a local
disk the files are in the system trash; on a NAS check the shared-folder recycle bin.

**Something was skipped.** Files that cannot be read (permissions, locks, vanished) are listed in
the report and the journal page; the run continues.

## Licensing

Framesift is MIT licensed. The desktop bundles include LGPL components as separate shared
libraries (Qt / PySide6, libheif, libde265, FFmpeg) and call ffmpeg, ffprobe and exiftool as
separate processes; see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports with the `framesift status --json` output
and a few lines of the log are the most useful thing you can send.
