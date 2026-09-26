# Framesift — Architecture

Status: **approved by the owner on 2026-09-25** (name Framesift, license MIT with `pi-heif`
pinned and an ffmpeg fallback). This document is the contract for milestones M1–M5 and is
updated whenever an implementation decision deviates from it.

> **Naming.** The project started as "PhotoSift", a name already used by several products
> (a Windows app in the Microsoft Store, RL Vision's PhotoSift, three GitHub projects).
> The owner chose **Framesift** (PyPI name free, no products found). Package `framesift`,
> CLI `framesift`, catalog folder `.framesift`, default folders `_framesift_review` and
> `_framesift_delete`, Docker image `ghcr.io/<owner>/framesift`.

Where the specification is silent, the decision taken here is marked **[D]** and
collected again in §21. Interpretations of ambiguous spec points are marked **[I]**.

---

## 1. Scope and hard rules

Framesift is a free desktop tool (macOS, Windows, Linux) plus a headless CLI for
sorting large phone-photo archives (200+ GB, 100k+ files, possibly on SMB). It has
three folders (Source, Review, Delete), a manual review mode, a group mode, an
automatic classifier that stages junk candidates into Review, and a folder browser.

Hard rules that every module must respect:

1. **Never modify file contents or write metadata.** Files are only renamed/moved.
2. **Never delete, except `purge`** (the "Empty Delete folder" button / CLI command
   behind an explicit confirmation). The only other `unlink` in the code base is the
   second half of a verified cross-volume move (§8.2), which is a move, not a delete.
3. **Never overwrite.** Name conflicts get a suffix and a journal record.
4. **Every move is journaled and undoable**, including after restart and from another
   machine that opens the same catalog.
5. **The UI never blocks.** All I/O and CPU work runs in worker threads/processes.
6. **Errors on individual files (permissions, locks, vanished files) are reported,
   never fatal.**

Out of scope for v1: transcoding, face recognition, AI classification, cloud, Apple
Photos / Google Photos / Lightroom integration, editing.

## 2. System overview

```
            ┌────────────────────┐        ┌────────────────────┐
            │  GUI  (PySide6)    │        │  CLI  (typer)      │
            │  framesift.gui     │        │  framesift.cli     │
            └─────────┬──────────┘        └─────────┬──────────┘
                      │  thin wrappers: no business logic
                      └──────────────┬───────────────┘
                              ┌──────▼──────┐
                              │   engine    │   pure Python, no Qt
                              │ framesift.  │   jobs, catalog, scanner,
                              │   engine    │   metadata, analysis,
                              └──────┬──────┘   classify, apply, undo, purge
          ┌──────────────────────────┼───────────────────────────┐
          ▼                          ▼                           ▼
   Source folder             Review folder                 Delete folder
   (never deleted from,      <Review>/<category>/...       <Delete>/<source-relative path>
    only moved out of)       <Review>/.framesift/          
                               catalog.db  lock  reports/  logs/  progress.json
```

Deployment scenario that drives the design: the archive lives on a Synology NAS. The
heavy pipeline (`scan`, `classify`, `apply`) runs in Docker **on the NAS** against the
local disk; the GUI on a Mac opens the same catalog over SMB and does the human part
(manual mode, group mode, undo, purge). Both sides see one catalog in
`<Review>/.framesift/`, so paths inside the catalog are stored relative to the three
roots and each host records its own absolute root paths (§6.4).

## 3. Repository layout

```
framesift/                     Python package (src layout not used: keeps PyInstaller simple)
  __init__.py                  __version__
  engine/                      NO Qt imports allowed (enforced by a test)
    config.py                  RootsConfig, ClassifyConfig (thresholds + defaults), constants
    paths.py                   NFC/NFD handling, long paths, excluded folders, root checks
    catalog.py                 SQLite connection, pragmas, lock file, migrations
    schema/0001_initial.sql …  versioned migrations, applied automatically
    scanner.py                 streaming os.scandir walk, incremental upsert, reconciliation
    metadata/                  header-only readers → MediaInfo
      __init__.py              dispatch by magic bytes / extension
      exif.py                  TIFF/EXIF helpers on top of Pillow, Apple MakerNote parser
      jpeg.py  png.py  webp.py  gif.py  tiff_dng.py
      isobmff.py               HEIF/HEIC + MOV/MP4/3GP box parser (meta, iloc, iprp, moov, keys/ilst)
      exiftool.py  ffprobe.py  optional external fallbacks (subprocess, batched)
    items.py                   companions (Live Photo MOV, .AAE, .XMP), edited pairs
    hashing.py                 size → partial (64 KB head+tail) → full BLAKE3
    imaging.py                 decode to PIL.Image (Pillow / pi-heif / ffmpeg), orientation, downscale
    analysis.py                pHash, sharpness, darkness, structure check; process pool
    classify/                  one module per category + pipeline.py (order, first-match, confidence)
    plan.py                    dry-run report model (counts, sizes, examples)
    apply.py                   plan → moves into Review, resumable
    fileops.py                 safe_move (rename / copy+verify), conflict suffixes, temp files
    journal.py                 journal rows, undo (last / session / category / all)
    purge.py                   THE ONLY MODULE THAT DELETES (trash or unlink)
    jobs.py                    Job runner: progress, ETA, pause/resume, cancel, checkpoints, low priority
    thumbs.py                  local LRU thumbnail cache
    reports.py                 CSV/JSON writers for full lists
    i18n.py                    message catalogs en/ru for engine strings (reasons, categories)
    external.py                locate bundled ffmpeg/ffprobe/exiftool (PyInstaller, Docker, PATH)
  cli/
    main.py                    typer app: scan, classify, report, apply, undo, purge, status, gui
  gui/
    app.py                     QApplication, theme, translator, single-instance guard
    main_window.py             navigation: Browser / Manual / Groups / Auto / Journal / Settings
    models/                    CatalogListModel (virtualized), FolderTreeModel, JournalModel
    views/                     browser.py manual.py groups.py auto.py journal.py settings.py
    workers/                   QThread bridges to engine jobs; ThumbnailLoader (QThreadPool)
    media/                     ImageView (QGraphicsView), VideoView (QMediaPlayer), FfmpegFrameView
    i18n/                      framesift_ru.ts / .qm (UI strings)
  resources/                   icons (own, MIT)
tests/
  fixtures/make_dataset.py     synthetic dataset generator (Pillow + ffmpeg)
  fixtures/bin/                a few tiny synthetic HEIC/HEVC files (no LGPL encoder exists)
  unit/  integration/  gui/  perf/
packaging/
  pyinstaller/*.spec  macos/  windows/  linux/appimage/  docker/Dockerfile
.github/
  workflows/ci.yml  release.yml  docker.yml
  ISSUE_TEMPLATE/  PULL_REQUEST_TEMPLATE.md
ARCHITECTURE.md  README.md  README.ru.md  CHANGELOG.md  CONTRIBUTING.md  LICENSE
THIRD_PARTY_LICENSES.md  pyproject.toml
```

Rules: `engine` imports nothing from `gui`/`cli`; `gui` and `cli` call the engine only
through `framesift.engine.api` (a small façade: `open_catalog`, `run_job`, `plan`,
`apply`, `undo`, `purge`, `list_*`). A unit test asserts that `import framesift.engine`
does not import `PySide6`.

## 4. Core concepts

| Term | Definition |
|---|---|
| **Root** | One of `source`, `review`, `delete`. Catalog paths are relative to a root. |
| **File** | One catalog row: a path inside a root with size/mtime and metadata. |
| **Item** | The unit the user acts on: a primary file plus its **companions**, always moved together. Companions: Live Photo MOV (paired by Apple ContentIdentifier, fallback same basename in the same folder), `.AAE` sidecar (same basename), `.XMP` sidecar (same basename) **[D]**. |
| **Edited version** | `IMG_E1234.*` next to `IMG_1234.*`: a separate item, linked to the original (`edited_of`), shown with an "edited" badge. |
| **Detached Live video** | A Live Photo MOV moved on its own by the explicit "Move all Live Photo MOVs to Review" action. Only this action breaks the companion rule, by design; undo re-attaches. |
| **Session** | One GUI run or one CLI invocation; groups journal rows for "undo session" and for the status-bar counters. |
| **Job** | A long-running engine operation (scan, analyze, classify, apply, undo, purge) with progress, checkpoints and pause/cancel. |
| **Candidate** | A file proposed by the classifier for a category, with a reason and confidence. |
| **Plan** | The dry-run report: candidates grouped by category with counts, sizes, examples. |

Formats: photos `jpg jpeg jpe heic heif hif png gif webp tif tiff`; RAW `dng cr2 cr3 nef arw
orf rw2 raf pef srw` (embedded preview only); videos `mov mp4 m4v 3gp 3g2`; sidecars
`aae xmp`. Everything else is `other`: listed by the scanner for the junk rules
(`.DS_Store`, `._*`, `Thumbs.db`, `desktop.ini`), otherwise ignored **[D]**.

## 5. Data flow

```
scan ──► files (paths, size, mtime)                      streaming, incremental
     ──► file_meta (header-only EXIF/QuickTime)          threads (I/O bound)
     ──► items (companions, edited pairs)
classify ──► file_analysis (hashes, pHash, sharpness…)   process pool, only what the rules need
         ──► candidates (category, reason, confidence, group, keeper)
report   ──► plan (from candidates + thresholds; no filesystem access)
apply    ──► moves to <Review>/<category>/…  + journal rows
manual / groups ──► moves (keep / to Delete / restore) + journal rows
undo     ──► reverse moves + journal rows
purge    ──► trash / unlink files in Delete + journal rows
```

`report` and the GUI's threshold sliders re-run only the cheap rule evaluation over
stored analysis results. The expensive stages (hashing, pHash, sharpness) are computed
once per file version (size+mtime) and reused.

## 6. Catalog (SQLite)

### 6.1 Location and access

* Path: `<Review>/.framesift/catalog.db`. Next to it: `lock`, `progress.json`,
  `reports/`, `logs/`, `tmp/`.
* `sqlite3` from the standard library, `PRAGMA foreign_keys=ON`, `synchronous=NORMAL`,
  `cache_size=-65536` (64 MB), `temp_store=MEMORY`.
* Journal mode: `WAL` when the catalog is on a local volume, `DELETE` when it is on a
  network volume (WAL needs coherent shared memory, which SMB/NFS do not provide).
  Volume type comes from `psutil.disk_partitions(all=True)` (fstype in
  `smbfs cifs nfs nfs4 afpfs webdav fuse.sshfs …`) or, on Windows, `GetDriveType ==
  DRIVE_REMOTE` / UNC path. The check is repeated on every open.
* Writes are batched: scan upserts in transactions of 500 rows; journal rows are
  committed one logical operation at a time (all files of one item), immediately after
  the filesystem operation succeeded.
* **Single writer.** `<Review>/.framesift/lock` is created with `O_CREAT|O_EXCL` and
  contains JSON `{host, pid, app, started, heartbeat}`. The owner refreshes `heartbeat`
  every 30 s. A lock whose heartbeat is older than 10 min is stale and may be taken
  over (the GUI asks; the CLI needs `--force-lock`). While another host holds the lock,
  the GUI opens **read-only**: browsing works, actions are disabled, and the NAS job's
  progress is shown from `progress.json` (rewritten every 2 s by the job runner, so
  the reader never touches the busy database).

### 6.2 Schema (version 1)

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- schema_version, catalog_uuid, created_at, created_by (app/version), source_layout

CREATE TABLE hosts (                      -- per-machine absolute paths of the three roots
  hostname   TEXT PRIMARY KEY,
  platform   TEXT NOT NULL,               -- darwin | win32 | linux
  source_path TEXT NOT NULL,
  review_path TEXT NOT NULL,
  delete_path TEXT NOT NULL,
  last_seen  TEXT NOT NULL
);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);   -- JSON values
-- include_subfolders, thresholds (ClassifyConfig), enabled categories, move_all_aae …
-- Shared by GUI and CLI: the same thresholds apply on the Mac and on the NAS.

CREATE TABLE files (
  id             INTEGER PRIMARY KEY,
  root           TEXT NOT NULL CHECK (root IN ('source','review','delete')),
  rel_path       TEXT NOT NULL,           -- NFC, '/' separators, relative to the root
  rel_path_os    TEXT,                    -- exact on-disk form when it differs from NFC (NFD etc.)
  origin_rel_path TEXT,                   -- source-relative path before Framesift moved it
  name           TEXT NOT NULL,           -- basename, NFC
  ext            TEXT NOT NULL,           -- lower-case, no dot
  kind           TEXT NOT NULL,           -- photo | raw | video | sidecar | other
  size           INTEGER NOT NULL,
  mtime_ns       INTEGER NOT NULL,
  item_id        INTEGER,                 -- id of the item's primary file (self for primaries)
  companion_role TEXT,                    -- NULL | live_video | aae | xmp
  edited_of      INTEGER,                 -- original's id for IMG_E* files
  detached_at    TEXT,                    -- Live video moved alone by the explicit action
  status         TEXT NOT NULL DEFAULT 'present',   -- present | missing | error
  error          TEXT,
  reviewed_at    TEXT,                    -- manual-mode "seen" mark
  first_seen     TEXT NOT NULL,
  last_seen      TEXT NOT NULL,
  seen_gen       INTEGER NOT NULL DEFAULT 0,   -- scan generation that last saw the file
  UNIQUE (root, rel_path)
);
CREATE INDEX files_item      ON files(item_id);
CREATE INDEX files_root_kind ON files(root, kind, status);
CREATE INDEX files_size      ON files(size);

CREATE TABLE file_meta (                  -- valid for (size, mtime_ns) it was extracted from
  file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  size         INTEGER NOT NULL,
  mtime_ns     INTEGER NOT NULL,
  format       TEXT,                      -- jpeg heic png gif webp tiff dng cr2 … mov mp4
  width        INTEGER, height INTEGER, orientation INTEGER,
  make         TEXT, model TEXT, software TEXT,
  date_taken   TEXT,                      -- ISO 8601, local time as written by the device
  date_source  TEXT,                      -- exif | quicktime | mtime
  user_comment TEXT,
  content_id   TEXT,                      -- Apple ContentIdentifier (MakerNote / QuickTime key)
  duration_ms  INTEGER, codec TEXT, bitrate INTEGER, fps REAL,
  has_preview  INTEGER,                   -- embedded thumbnail/preview found
  camera_known INTEGER NOT NULL,          -- 1 = format parsed, so NULL make/model means "absent"
  extractor    TEXT NOT NULL,             -- jpeg | isobmff | png | … | exiftool | ffprobe | none
  extracted_at TEXT NOT NULL
);
CREATE INDEX file_meta_content ON file_meta(content_id);
CREATE INDEX file_meta_date    ON file_meta(date_taken);

CREATE TABLE file_analysis (
  file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  size         INTEGER NOT NULL,
  mtime_ns     INTEGER NOT NULL,
  partial_hash TEXT,                      -- BLAKE3 of first 64 KB + last 64 KB (hex)
  full_hash    TEXT,                      -- BLAKE3 of the whole file (hex)
  phash        INTEGER,                   -- 64-bit perceptual hash
  phash_source TEXT,                      -- exif_thumb | heif_thumb | decode
  sharpness    REAL,                      -- Laplacian variance on the small representation
  brightness   REAL, dark_p99 REAL,       -- mean and 99th percentile luminance, 0..1
  structure_ok INTEGER,                   -- magic bytes + trailer sanity (EOI / IEND / moov)
  decode_error TEXT,
  analyzed_at  TEXT
);
CREATE INDEX file_analysis_partial ON file_analysis(partial_hash);
CREATE INDEX file_analysis_full    ON file_analysis(full_hash);

CREATE TABLE sessions (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,               -- manual | auto | cli | undo | purge
  host       TEXT NOT NULL,
  app        TEXT NOT NULL,               -- "gui 0.1.0" / "cli 0.1.0"
  started_at TEXT NOT NULL,
  ended_at   TEXT,
  params     TEXT                         -- JSON
);

CREATE TABLE jobs (
  id          INTEGER PRIMARY KEY,
  session_id  INTEGER REFERENCES sessions(id),
  kind        TEXT NOT NULL,              -- scan | analyze | classify | apply | undo | purge
  state       TEXT NOT NULL,              -- running | paused | done | cancelled | failed
  params      TEXT, progress TEXT, checkpoint TEXT,   -- JSON
  started_at  TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT,
  error       TEXT
);

CREATE TABLE candidates (
  id            INTEGER PRIMARY KEY,
  run_id        INTEGER NOT NULL REFERENCES jobs(id),
  file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  category      TEXT NOT NULL,
  confidence    TEXT NOT NULL,            -- high | medium | low
  reason_key    TEXT NOT NULL,            -- i18n key, e.g. "reason.duplicate_of"
  reason_params TEXT,                     -- JSON, e.g. {"keeper": "2019/IMG_0012.HEIC"}
  group_id      TEXT,                     -- duplicates / similar group
  is_keeper     INTEGER NOT NULL DEFAULT 0,
  also          TEXT,                     -- JSON list of other categories that matched
  applied_op    TEXT,                     -- journal.op_id once moved
  UNIQUE (run_id, file_id)
);

CREATE TABLE journal (                    -- see §7
  id            INTEGER PRIMARY KEY,
  op_id         TEXT NOT NULL,            -- one logical user/auto action (all files of an item)
  session_id    INTEGER NOT NULL REFERENCES sessions(id),
  ts            TEXT NOT NULL,
  action        TEXT NOT NULL,            -- keep | to_delete | to_review | restore | detach_live | purge | undo
  file_id       INTEGER NOT NULL,
  item_id       INTEGER NOT NULL,
  from_root TEXT, from_rel TEXT, to_root TEXT, to_rel TEXT,
  method        TEXT,                     -- rename | copy_verify | none | trash | unlink
  conflict      INTEGER NOT NULL DEFAULT 0,
  category      TEXT, reason_key TEXT, reason_params TEXT,
  size          INTEGER NOT NULL,
  undoes        INTEGER REFERENCES journal(id),
  undone_by     INTEGER REFERENCES journal(id)
);
CREATE INDEX journal_session  ON journal(session_id);
CREATE INDEX journal_file     ON journal(file_id);
CREATE INDEX journal_category ON journal(category);
CREATE INDEX journal_op       ON journal(op_id);

CREATE TABLE issues (                     -- skipped files, access errors, conflicts, stale temp files
  id INTEGER PRIMARY KEY, job_id INTEGER, ts TEXT NOT NULL,
  path TEXT, kind TEXT NOT NULL, message TEXT
);
```

### 6.3 Migrations

`meta.schema_version` is compared with the newest `schema/NNNN_*.sql` on open; missing
migrations run in order inside one transaction each. Downgrade is refused with a clear
message (an older app opening a newer catalog). A backup copy `catalog.db.bak-<version>`
is written before any migration.

### 6.4 Machine-independent paths

The catalog knows the Review root implicitly (its own parent directory). For Source and
Delete it stores, in `meta.source_layout`, the layout relative to Review when possible
(`{"source": "..", "delete": "../_framesift_delete"}`, which is the default layout) and,
in `hosts`, each machine's absolute paths. Resolution order on open: `hosts` row for
this hostname → relative layout → ask the user (GUI) / require `--source` (CLI).
`--source`/`--delete-dir` given explicitly always win and update `hosts`.

## 7. Journal and undo

One journal row per file per action; rows of one logical action share `op_id`
(an item with companions produces 2–3 rows). Fields: where from, where to, when, which
action, method used, whether a conflict suffix was added, the category/reason for
automatic moves, and the session.

Action semantics (manual mode, spec §3.3), all journaled:

| Item is in | `→` keep | `←` to Delete |
|---|---|---|
| Source | `keep`: no move, `reviewed_at` set | `to_delete`: move to `<Delete>/<source-relative path>` |
| Review | `restore`: move back to `origin_rel_path` in Source, `reviewed_at` set | `to_delete`: move to `<Delete>/<origin_rel_path>` |
| Delete | `restore`: move back to `origin_rel_path` in Source | no-op |

Automatic mode records `to_review` (category + reason). The explicit Live Photo action
records `detach_live`. `purge` records `purge` rows with `method = trash | unlink`.

**Undo** of a row = the reverse move (`to` → `from`), recorded as a new row with
`action='undo'`, `undoes=<id>`; the original row gets `undone_by`. Undo of `keep`
clears `reviewed_at`. Scopes:

* *last action* — the newest not-undone `op_id` of the current session (multi-level:
  repeat as often as wanted);
* *session*, *category*, *everything* — all not-undone ops matching the scope, newest
  first, as one job with progress and a result summary.

Undo works after restart and from another machine: it only needs the catalog and the
roots. Undo of a `purge` row is impossible and the UI says so. If the original location
is occupied at undo time, the conflict suffix rule applies and the row is marked
`conflict=1`. Redo is not offered in v1 **[D]**.

Files found in Review/Delete without any journal history (put there by hand) have no
`origin_rel_path`; for them "restore" is disabled and "keep" only marks them reviewed **[D]**.

## 8. File operations (`fileops.py`)

### 8.1 Same volume

`os.rename` (atomic, preserves mtime and attributes). Before that, `dst.parent` is
created with `mkdir(parents=True, exist_ok=True)`.

### 8.2 Cross volume

`os.rename` raising `EXDEV` (or the up-front `st_dev` / Windows volume-path check) →
`copy_verify`: stream-copy to `<dst>.framesift-part` while computing BLAKE3, `fsync`,
`shutil.copystat` (mtime, permissions), re-read the copy and compare hashes, rename the
temp file to `dst`, and only then `unlink` the source. Leftover `*.framesift-part`
files from a crash are removed at the next start and logged to `issues`. The move
returns `method='copy_verify'`; the GUI warned about slowness at folder setup already.

### 8.3 Name conflicts

Never overwrite. If `dst` exists, use `name~2.ext`, `name~3.ext`, … (`~N` is chosen
because ` (1)`, `copy`, `-1` are exactly the duplicate markers the classifier looks
for) **[D]**. All companions of the item get the same suffix so they stay paired by
basename. `journal.conflict=1` and an `issues` row record it.

### 8.4 Paths

* Catalog paths are NFC with `/` separators. When the directory listing returns a
  different form (NFD from macOS/SMB), it is kept in `rel_path_os`; OS calls always use
  `rel_path_os or rel_path`. Lookups compare NFC forms, so the same file listed as NFD on
  the Mac and NFC on the NAS is one row.
* Windows: every absolute path passed to the OS is prefixed with `\\?\` (or `\\?\UNC\`)
  once it exceeds 240 characters; forward slashes are normalized.
* Symlinks are not followed and not listed **[D]**.
* Excluded from scanning: `@eaDir #recycle #snapshot .Trashes .Spotlight-V100 .fseventsd
  $RECYCLE.BIN "System Volume Information" .framesift .thumbnails @Recycle`, the Review
  and Delete roots (wherever they are), `*.photoslibrary` and `*.lrdata` subtrees (with a
  warning). A Source that is, or is inside, a `*.photoslibrary` is refused. A folder
  containing `*.lrcat` produces a Lightroom warning only.

### 8.5 Failure handling

Every per-file exception (permission denied, sharing violation, ENOENT, EIO) is caught,
written to `issues` and the job log, counted in the job's `progress.skipped`, and the
job continues. A whole subtree failing to list does not mark its known files as
missing.

## 9. Scanning and metadata

### 9.1 Walk

Iterative `os.scandir` (no recursion limit), per-directory batches, results streamed to
the catalog in 500-row transactions and to GUI listeners (so the browser shows a fresh
folder within the first second). The walk covers all three roots (Review and Delete are
catalogued too, for the tree counters and reconciliation).

Incremental rule: a row whose `(rel_path, size, mtime_ns)` is unchanged is only touched
(`last_seen`, `seen_gen`); a changed one invalidates `file_meta`/`file_analysis` (they
carry the size/mtime they were computed for); rows whose `seen_gen` is older than the
current scan generation and whose subtree was listed **without errors** become
`status='missing'` and are reported, never deleted from the catalog (their journal
history must survive).

### 9.2 Header-only metadata readers **[D]**

The spec allows exiftool/ffprobe; this design makes pure-Python readers the primary
path and keeps exiftool/ffprobe as fallbacks. Reasons: no process spawn per file (100k
spawns over SMB is the bottleneck), exact control over how many bytes are read, and a
Docker image that works even if the external binaries are missing.

| Format | Reader | Bytes read | What is extracted |
|---|---|---|---|
| JPEG | `jpeg.py` walks APPn segments up to SOS (head chunk 256 KB, grown on demand), EXIF parsed with `PIL.Image.Exif.load` | head + 64 KB tail | Make, Model, Software, DateTimeOriginal (+OffsetTime), UserComment, dimensions (SOF), Orientation, MakerNote → Apple ContentIdentifier, EXIF thumbnail (for pHash), trailer check (EOI) |
| HEIC/HEIF | `isobmff.py`: `ftyp`, `meta` (`pitm`, `iinf`, `iloc`, `iref`, `iprp/ipco` + `ipma`) | head 64 KB + one seek/read for the Exif item | dimensions from `ispe` of the primary (grid) item, rotation `irot`/`imir`, codec from `hvcC`, Exif item → same fields as JPEG, thumbnail item location (`thmb` reference) |
| MOV/MP4/M4V/3GP | `isobmff.py`: `moov` (head or tail), `mvhd`, `trak/tkhd`, `stbl/stsd`, `moov/meta` + `udta/meta` `keys`/`ilst` | head 64 KB, tail 64 KB, plus the `moov` box | duration, creation date, dimensions, rotation matrix, codec fourcc (`hvc1/hev1/avc1/mp4v`), fps, bitrate (size·8/duration), Apple keys `com.apple.quicktime.{make,model,software,creationdate,content.identifier,live-photo.auto}`, Android `com.android.version` |
| PNG | `png.py` chunk walk: `IHDR`, `tEXt/iTXt`, `eXIf`; if no `eXIf` before `IDAT`, scan the 64 KB tail for `eXIf` (iOS writes it after the image data) | head + tail | dimensions, EXIF (UserComment "Screenshot"), trailer check (IEND) |
| WebP / GIF | RIFF / GIF header parse | head | dimensions, EXIF chunk if present (WebP) |
| TIFF / DNG | Pillow lazy TIFF open (IFD0 + Exif IFD) | IFDs only | camera fields, dimensions, preview SubIFD location |
| RAW (CR2, NEF, ARW, …) | exiftool when available (`-j -fast2 -stay_open` batch); otherwise `camera_known=0` | exiftool decides | camera fields, embedded preview (`-PreviewImage -b`) for thumbnails |
| anything the reader rejects | `ffprobe -print_format json` for video, exiftool for images, when available | — | fallback; `extractor` records which reader won |

`camera_known` is the key to safe classification: only formats we actually parsed can
be "without camera data". A RAW file without exiftool is never a `no_camera_media`
or `junk` candidate.

Apple MakerNote: bytes begin with `Apple iOS\0`, version `\0\x01`, byte order `MM`,
then an IFD at offset 14 with value offsets relative to the MakerNote start; tag
`0x0011` is ContentIdentifier (ASCII). Live Photo pairing: photo `content_id` ==
MOV `com.apple.quicktime.content.identifier`; global match by id, preferring the same
folder; fallback: same basename in the same folder with a photo + MOV pair **[I]**.

`date_taken` = EXIF DateTimeOriginal → QuickTime creation date → file mtime, with
`date_source` recorded, so sorting and the similar-window always have a value **[D]**.

Metadata extraction runs in a thread pool (I/O bound, default 8 threads, 16 on network
volumes) after the walk of each directory batch.

## 10. Analysis (`analysis.py`)

Computed lazily: only for files a rule needs, only once per file version, in a
`ProcessPoolExecutor` (`--workers`, default `cpu_count - 1`, min 1).

* **Exact duplicates**: group by `size` (from the scan) → `partial_hash` (BLAKE3 over
  first 64 KB + last 64 KB) for sizes with ≥ 2 files → `full_hash` (BLAKE3, 1 MB
  chunks) only for partial-hash collisions.
* **Structure check** (`structure_ok`): magic bytes match the extension family, and the
  trailer is sane (JPEG `FFD9` in the last 64 KB, PNG `IEND`, ISOBMFF `moov` found).
  Cheap, computed during metadata extraction because the tail is already read.
* **Small representation**: ≤ 256 px on the long side, grayscale, EXIF orientation
  applied, obtained in this order: EXIF thumbnail (JPEG) → HEIF thumbnail item
  (decoded by handing pi-heif a minimal in-memory HEIF built from the item's `hvcC`
  and data, or by ffmpeg) → `Image.draft` reduced JPEG decode → full decode.
  The source is recorded (`phash_source`).
* **pHash**: in-house NumPy implementation of the imagehash algorithm (32×32 grayscale,
  DCT-II via a precomputed matrix, top-left 8×8, median threshold → 64 bits). No
  SciPy/imagehash dependency **[D]**.
* **Sharpness**: variance of the 3×3 Laplacian on the small representation.
  **Darkness**: mean luminance and 99th percentile. Computing on ≤ 256 px trades
  sensitivity for speed (100k HEIC full decodes would take hours); the thresholds are
  conservative accordingly, and the category is low confidence.
* Decode failures are stored in `decode_error` and feed the junk rule.

Low priority mode: `psutil` sets nice 15 + `ionice` idle/low on Unix,
`BELOW_NORMAL_PRIORITY_CLASS` + low I/O priority on Windows, for the job process and
all workers.

## 11. Classification

Pure functions over catalog rows. Order and first-match-wins as in the spec; other
matching categories are stored in `candidates.also` and shown in the reason.

| # | Category | Confidence | Rule (defaults in `ClassifyConfig`) | What stays |
|---|---|---|---|---|
| 1 | `duplicates` | high | equal `full_hash` among primary files (companions follow their item) | keeper = cleanest name (penalties for ` (1)`, ` copy`, `-1`, `_1`, `Copy of `, ` 2`), then earliest `date_taken`, then shortest path |
| 2 | `screenshots` | high | `user_comment == "Screenshot"` (case-insensitive) OR (PNG, `camera_known`, no Make/Model, `(w,h)` in the phone screen table, either orientation) | — |
| 3 | `screen_recordings` | high | name matches `RPReplay_Final\d+` OR (video, no camera keys, `(w,h)` in the screen table) | — |
| 4 | `short_videos` | medium | `duration_ms < short_video_seconds·1000` (default 3), not a Live Photo companion | — |
| 5 | `no_camera_media` | low | photo/video with `camera_known=1` and empty Make and Model and no Android/Apple video keys, not matched above | — |
| 6 | `junk` | high | name in `.DS_Store ._* Thumbs.db desktop.ini`; `size == 0`; `structure_ok = 0` or `decode_error`; orphan `.AAE` (no sibling with the same basename); option `move_all_aae` (off) | — |
| 7 | `blurry_dark` | low | `sharpness < blur_threshold` (default 12.0, Laplacian variance on the 0–255 scale of the 256 px representation) OR (`brightness < 0.03` AND `dark_p99 < 0.12`) | — |
| 8 | `similar` | medium | photos with `date_taken`, sorted; pairs within `similar_window_s` (default ±60) and Hamming(pHash) ≤ `similar_distance` (default 6 of 64) → union-find groups; original/edited pairs and companions are never grouped with each other | keeper = highest sharpness, then most pixels |

The phone screen table (`config.py`) lists iPhone resolutions (iPhone 4 … 17 families,
portrait dimensions; matched in both orientations) and a short generic Android list;
adding a device is one line.

Report-only (no moves): `live_photos` (count/size of Live Photo MOVs + the explicit
"Move all Live Photo MOVs to Review" action with its warning), `edited_pairs`
(count/size), `large_videos` (top 100 by size with codec, resolution, bitrate; for
H.264 an estimate `size × 0.45` labelled as rough HEVC savings).

Thresholds live in `settings` (shared through the catalog), are exposed as CLI options
and as GUI sliders, and re-evaluation after a slider change needs no filesystem access
(only pHash/sharpness that were never computed trigger a short analysis job).

## 12. Apply and the Review layout

* `<Review>/<category>/<source-relative path>`; `similar` uses
  `<Review>/similar/<group_id>/<name>` with `group_id = s<run>-<n>`.
* Keepers stay in Source; group mode shows keeper + moved members through `group_id`.
* Dry-run first, always: `plan()` returns the report; `apply()` takes the confirmed
  category set. `apply` iterates candidates with `applied_op IS NULL`, moves one item at
  a time (all companions), writes the journal rows, and advances `jobs.checkpoint`;
  pause/cancel act between items; a killed process resumes from the checkpoint.
* Progress: items done/total, bytes, rate, ETA (moving average over the last 30 s).

## 13. Thumbnails, previews and decoding

* Cache root: `platformdirs.user_cache_dir("framesift")` — never inside user folders.
  Layout `thumbs/<catalog_uuid>/<id % 256>/<id>.jpg`, JPEG q85, 320 px long side;
  index in a local SQLite `thumbs/index.db` (`catalog_uuid, file_id, size, mtime_ns,
  bytes, last_access`). LRU eviction to the configured limit (default 2 GB) runs in the
  background when the limit is exceeded by 5 %.
* Sources, cheapest first: EXIF thumbnail (JPEG) / HEIF thumbnail item / DNG or RAW
  embedded preview / `Image.draft` reduced decode / full decode. Videos: one frame at
  `min(1 s, 10 %)` via `ffmpeg -ss … -frames:v 1 -f image2pipe` (LGPL build); Live Photo
  MOVs reuse the photo's thumbnail.
* Full-size images for manual mode: decoded in a worker at `min(full, 2 × viewport)`
  (JPEG uses DCT scaling), EXIF orientation applied; ±5 items prefetched within a memory
  budget (default 600 MB); `Z` decodes full resolution on demand.
* HEIC decoding: **pi-heif** (libheif + libde265, LGPL-3.0 wheels), with an
  **ffmpeg fallback** (FFmpeg ≥ 7.1 demuxes HEIF incl. tiled grids and decodes HEVC with
  its native LGPL decoder). Both paths are tested. See §17 for why not pillow-heif.

## 14. GUI (`framesift.gui`)

* PySide6 widgets, Fusion style on Windows/Linux, native on macOS; light/dark from
  `QStyleHints.colorScheme` (Qt ≥ 6.5) with a palette pair; i18n through Python
  dictionaries (`gui/i18n.py`, keyed by the English string, and the engine's catalogs for
  reasons/categories) instead of `.ts/.qm` files: no build step, testable, one language
  switch for GUI and engine **[D]**; language from the system locale unless overridden in
  Settings.
* Threads: the main thread only paints. `QThreadPool` for thumbnail loading (priority to
  visible rows, cancelled when scrolled away), metadata peeks and single moves; engine
  jobs run in a `QThread` that forwards progress signals; pixel analysis stays in the
  engine's process pool.
* **Browser**: left `QTreeView` (Source subfolders; Review categories with count/size;
  Delete with count/size; counters updated from journal signals), centre `QListView` in
  icon mode over `CatalogListModel` (ids only in memory, row cache LRU 10k, uniform
  item sizes, batched layout → smooth at 100k), sort by date/name/size, filter
  photo/video/all, selection by click/Shift/Ctrl-Cmd/rubber band, actions Keep / To
  Delete / Restore, double-click/Enter opens manual mode at the item with the same
  sort and filter.
* **Manual mode**: `QGraphicsView` image (zoom `Z`, drag to pan), `QGraphicsVideoItem`
  + `QMediaPlayer`/`QAudioOutput` for video (Space, M), `L` hold plays the Live Photo
  MOV, collapsible info panel (`I`), keys `→ ← ↓ Cmd/Ctrl+Z Esc`, buttons with key
  hints, status bar `123 / 4567 · kept · to delete · GB to free`, "Show reviewed"
  toggle. Video fallback: if `QMediaPlayer` reports an error for HEVC, a
  `FfmpegFrameView` streams frames from `ffmpeg -f rawvideo` (no audio, clearly labelled).
* **Group mode**: strip of the group + large selected frame; `← → K Enter ↓ Cmd/Ctrl+Z`;
  keeper pre-marked; `Enter` = marked items keep (Review members are restored to
  Source), unmarked go to Delete **[I]**.
* **Auto mode**: pipeline page (scan → classify → report → apply) with progress, ETA,
  pause, resume after restart, cancel; report table with per-category checkbox, count,
  size, confidence, 12 example thumbnails, threshold sliders, low-priority toggle,
  worker count; "Move all Live Photo MOVs" button with its warning.
* **Journal**: table with search and filters (session, category, action, path), undo
  last / session / category / all.
* **Settings**: three folders with defaults, "Include subfolders", volume check with the
  copy warning, Apple Photos refusal / Lightroom warning, per-source memory
  (`platformdirs.user_config_dir`, keyed by catalog uuid), language, cache limit.
* **Purge**: dialog with count and size; local volume → `send2trash`; network volume →
  the irreversible warning naming the NAS shared-folder recycle bin and snapshots.

## 15. CLI (`framesift.cli`)

```
framesift scan     --source DIR [--review DIR] [--delete-dir DIR] [--recursive/--no-recursive]
                   [--workers N] [--low-priority] [--log-file FILE] [--json]
framesift classify [--catalog DIR] [--categories a,b,…] [--short-video-seconds 3]
                   [--similar-window 60] [--similar-distance 6] [--blur-threshold 12.0]
                   [--dark-threshold 0.03] [--move-all-aae] [--workers N] [--low-priority]
framesift report   [--catalog DIR] [--json] [--out DIR]
framesift apply    [--catalog DIR] [--categories …] [--live-photos] [--yes]     # dry-run by default
framesift undo     [--catalog DIR] (--last | --session ID | --category NAME | --all) [--yes]
framesift purge    [--catalog DIR] [--yes]                                      # dry-run by default
framesift status   [--catalog DIR]          # counts, last jobs, lock owner, catalog version
framesift gui      [DIR]                    # launches the GUI when the gui extra is installed
```

`--catalog` is the Review folder (or the `.db` path); with only `--source` the default
Review/Delete folders are used and remembered in `hosts`. `undo` is dry-run by default
too, for symmetry with `apply` **[D]**.

Output discipline (agents will drive this): stdout shows totals and up to 5 examples per
category; `--json` prints one JSON document; the full lists go to
`<Review>/.framesift/reports/<UTC timestamp>-<command>/report.json` plus one CSV per
category; progress goes to `<Review>/.framesift/logs/framesift-<date>.log` (or
`--log-file`), one line every 2 s, `tail -f`-friendly. Exit codes: 0 ok, 1 error,
2 usage, 3 lock held, 4 refused (Apple Photos library), 5 partial (some files skipped).

## 16. NAS and Docker

* Image `ghcr.io/<owner>/framesift:<tag>` (and `:latest`, `:edge`), `linux/amd64` +
  `linux/arm64`, base `python:3.12-slim`, CLI only (no Qt), with ffmpeg/ffprobe (BtbN
  LGPL static build, pinned, checksum verified) and exiftool (Debian
  `libimage-exiftool-perl`). Entrypoint `framesift`.
* Run as the user's uid:gid (`--user 1026:100` on Synology), mount the shared folder,
  `--low-priority` inside plus `--cpu-shares` outside. README (M5) gives Container
  Manager and SSH `docker run` recipes.
* The catalog written on the NAS is opened by the Mac GUI over SMB: read-only while the
  NAS job holds the lock, full access afterwards; `hosts` maps the roots.

## 17. Dependencies and licenses

Versions are the current PyPI releases (checked 2026-09-25).

| Component | Version | License | Role / notes |
|---|---|---|---|
| Python | 3.12 | PSF-2.0 | runtime |
| Pillow | 12.3 | MIT-CMU | JPEG/PNG/GIF/WebP/TIFF decoding, EXIF parsing |
| **pi-heif** | 1.4.0 (final) | BSD-3 (wheels: LGPL-3.0 — libheif 1.18.1, libde265 1.0.15) | HEIC decoding. See the license note below. |
| numpy | 2.x | BSD-3 | pHash, sharpness, darkness |
| blake3 | 1.0 | CC0-1.0 OR Apache-2.0 | hashes |
| typer (+ click, rich) | 0.27 | MIT (BSD-3, MIT) | CLI |
| psutil | 7.2 | BSD-3 | volume types, nice/ionice |
| send2trash | 2.1 | BSD-3 | system trash for purge |
| platformdirs | 4.11 | MIT | cache/config dirs |
| PySide6 | 6.11 | LGPL-3.0 (dynamic linking; Qt Multimedia's FFmpeg is an LGPL build) | GUI only (`framesift[gui]`) |
| pyobjc-framework-Cocoa | 11.x | MIT | macOS only: NSFileManager trash API for send2trash |
| ffmpeg / ffprobe | 7.1+ | LGPL-2.1+ (builds configured without `--enable-gpl`/`--enable-nonfree`) | video thumbnails, ffprobe fallback, HEIC fallback, playback fallback |
| exiftool | 13.x | Perl Artistic-1.0 / GPL-1.0+ (dual); separate process | optional: RAW metadata/previews, metadata fallback |
| pytest, pytest-qt, ruff, mypy | — | MIT | dev only |
| PyInstaller | 6.22 | GPL-2.0+ **with bootloader exception** | build only; the exception explicitly allows bundling MIT apps |

Not used, and why: `pillow-heif` (its wheels bundle **x265, GPL-2.0**, so the wheel is
GPL-2.0 — see `LICENSES_bundled.txt` inside the wheel), `imagehash` (pulls SciPy;
trivial to reimplement), `rawpy` (LibRaw is fine license-wise but heavy; exiftool
previews cover v1), `PyAV`/`imageio-ffmpeg` (their bundled FFmpeg builds are GPL).

**License conclusion: MIT is feasible.** Obligations: ship `THIRD_PARTY_LICENSES.md`
with the LGPL texts and notices (Qt, libheif, libde265, FFmpeg), keep all LGPL code as
separate shared libraries (PyInstaller one-dir bundles do this; users can swap them),
include the exiftool notice, and note that FFmpeg is called only as a separate process.

**The one open license item — HEIC decoding.** `pi-heif` is the GPL-free build of
pillow-heif and its 1.4.0 release (June 2026) is declared final. It still ships wheels
for Python 3.10–3.14 on all our targets. The plan above pins it and keeps the ffmpeg
HEIC path as a tested fallback, so the app does not depend on pi-heif's future; if a
newer libheif is ever needed, pillow-heif's own build scripts can produce a
decoder-only wheel in our CI. The alternative is `pillow-heif` + GPL-3.0 for the whole
project. The owner chose the first option (§23).

## 18. Packaging and CI

* **PyInstaller one-dir** bundles from `packaging/pyinstaller/*.spec`; macOS `.app` in a
  `.dmg` (`hdiutil`), Windows `.zip`, Linux `.AppImage` (`appimagetool`). Artifacts:
  `Framesift-<ver>-macos-arm64.dmg`, `-macos-x86_64.dmg`, `-windows-x64.zip`,
  `-linux-x86_64.AppImage`.
* **External binaries** (`packaging/fetch_binaries.py`): ffmpeg/ffprobe from BtbN's
  `master-latest-*-lgpl` static builds for Windows and Linux (the `latest` release only
  carries master snapshots; they are ≥ 7.1 and decode HEIF grids, verified); for macOS,
  FFmpeg 7.1.1 is compiled in the release workflow from source with `--disable-gpl
  --disable-nonfree --enable-videotoolbox` (cached per version). exiftool: the version is
  read from `exiftool.org/ver.txt` at build time; the official Windows 64-bit package and
  the Perl distribution for macOS/Linux, run with the system `perl` (present on macOS and
  practically every Linux); if `perl` is missing the app degrades to "RAW previews
  unavailable".
* **CI (`ci.yml`)**: on every PR, matrix ubuntu/windows/macos × Python 3.12 with the
  `gui` extra: ruff, mypy (engine + CLI), pytest with `QT_QPA_PLATFORM=offscreen`; ffmpeg
  from apt / brew / the BtbN LGPL build on Windows.
* **Release (`release.yml`)**: on tag `v*`: four bundles (macOS arm64 on `macos-14`,
  Intel on the current Intel runner label), Docker buildx for amd64+arm64 → GHCR,
  draft GitHub Release with the CHANGELOG section attached.
* **Signing**: if `APPLE_CERTIFICATE_P12`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_TEAM_ID`,
  `APPLE_ID`, `APPLE_APP_PASSWORD` exist → `codesign` + `notarytool` + staple; if
  `WINDOWS_CERT_PFX`/`WINDOWS_CERT_PASSWORD` exist → `signtool`. Otherwise unsigned,
  with README instructions (macOS: right-click → Open or `xattr -d
  com.apple.quarantine`; Windows: SmartScreen "More info → Run anyway").

## 19. Testing

* `tests/fixtures/make_dataset.py` generates the synthetic set from the spec (JPEG/PNG
  with and without camera EXIF, screenshots with UserComment, duplicates with ` (1)`,
  Live Photo pairs sharing a ContentIdentifier, short MP4/MOV via `ffmpeg -f lavfi`, `.AAE`,
  zero-byte and corrupt files, Cyrillic/space/NFD names). HEIC and HEVC samples cannot
  be produced with an LGPL encoder, so `tests/fixtures/bin/` holds a handful of tiny
  synthetic ones (generated once from a solid-colour image; provenance documented). The
  generator can scale to 50k files for the performance run.
* Unit tests per category, per reader (including a hand-built Apple MakerNote and a grid
  HEIC), path normalization, conflict suffixes, excluded folders, lock/stale lock,
  migrations.
* Integrity test: apply all categories → undo all → tree identical by paths, sizes,
  hashes and mtimes. Cross-volume test with a second temp filesystem (tmpfs on Linux,
  a RAM disk or a second drive letter when available; else `EXDEV` is injected).
* Safety tests: dry-run leaves the filesystem byte-identical; no command except `purge`
  reduces the set of file contents (hash multiset before/after); the engine never
  imports Qt.
* GUI smoke (pytest-qt, offscreen): start, open folder, `→ / ← / Cmd+Z`, group mode
  keys, journal undo.
* Performance run (`tests/perf/`): 50k synthetic files — scan time, RSS, first-thumbnail
  latency, next-item latency; numbers reported in the M5 release notes.

## 20. Performance plan

| Target (spec §5) | Approach |
|---|---|
| Start < 3 s | lazy imports (numpy, media, workers), no catalog work before the window shows |
| 50k folder: first thumbnails < 1 s | model holds ids only; rows and thumbnails stream; walk results appear per directory batch |
| Next item < 100 ms | ±5 prefetch, decoded pixmaps in memory, thumbnail shown while the full image decodes |
| RSS < 1.5 GB at 100k | id lists, row LRU 10k, prefetch budget 600 MB, thumbnail pixmap cache 200 MB, workers in separate processes |
| SMB | header-only readers with one large read instead of many small ones; hashing only for size collisions; pixel stages meant to run on the NAS |
| Catalog on SMB | batched transactions, 64 MB page cache, `DELETE` journal, `progress.json` for readers |

### 20.1 Measured (v0.1.0, `tests/perf/bench.py`, 50,000 synthetic JPEGs + the test dataset, Linux container, 3 workers)

| Step | Result | Target |
|---|---:|---|
| `scan` first run (walk + metadata) | 18.3 s | — |
| `scan` again, nothing changed | 1.4 s | — |
| `classify` all categories (partial hashes for every size collision, pixel metrics for every photo) | 29.8 s, peak RSS 500 MB | < 1.5 GB at 100k |
| `report` / `apply --dry-run` | 0.3 s | — |
| catalog size | 35 MB | — |
| GUI window shown | 0.08 s after imports (≈ 1 s process start) | < 3 s |
| open a 50k-item folder → list + first thumbnail | 0.47 s | < 1 s |
| next item in manual mode (prefetched) | 2 ms | < 100 ms |

The classify peak is dominated by the in-memory row list (three loads of ~50 columns × 50k
rows); at 100k files it stays within the budget but it is the first thing to slim down (load
only the columns the rules need).

## 21. Decisions where the specification is silent

1. Metadata readers are pure Python; exiftool/ffprobe are optional fallbacks (§9.2).
2. `.XMP` sidecars are companions like `.AAE`.
3. Symlinks are neither followed nor listed.
4. `date_taken` falls back to QuickTime creation date, then file mtime (`date_source` kept).
5. Conflict suffix is `~N`, applied to the whole item.
6. Files placed in Review/Delete by hand have no origin: restore disabled, keep = mark reviewed.
7. No redo; undo of an undo is not offered.
8. Sharpness/darkness are computed on a ≤ 256 px representation; thresholds are conservative.
9. pHash is an in-house NumPy implementation of the imagehash algorithm (no SciPy).
10. Original/edited pairs and companions are never grouped as "similar".
11. Videos are never sent to `blurry_dark`.
12. RAW files (and anything not parsed) are never `no_camera_media`/`junk` (`camera_known`).
13. Android video metadata (`com.android.version`) counts as camera evidence.
14. `undo` in the CLI is dry-run by default, like `apply` and `purge`.
15. Thresholds and category toggles live in the catalog (shared GUI/CLI); UI preferences live locally.
16. Thumbnail cache key is `(catalog uuid, file id, size, mtime)`, so thumbnails survive moves.
17. Review/Delete roots are catalogued too, so tree counters and reconciliation work without special cases.
18. `*.photoslibrary` and `*.lrdata` subtrees inside Source are skipped with a warning (a Source at or inside a library is refused, as specified).
19. The GUI opens a catalog read-only while another host holds the lock, showing that job's progress.
20. Group mode `Enter` restores marked Review members to Source (the "keep" semantics of the manual-mode table).
21. Docker uses the same LGPL FFmpeg static build as the desktop bundles, for a uniform license story.
22. GUI strings are translated through Python dictionaries, not Qt `.ts/.qm` files.
23. Manual-mode actions run on a single background thread in submission order; the view advances
    immediately and reconciles on failure (keeps "next item < 100 ms" on slow network volumes).
24. The session counters are net figures: an item restored later no longer counts as "to delete".
25. Qt is touched only on the GUI thread. Engine jobs run on one `threading.Thread` each;
    thumbnails, image decodes and file actions on `concurrent.futures` pools. Worker code holds
    only plain Python objects and puts its results on a queue that a GUI-thread timer drains,
    emitting the Qt signals there. Python's cyclic garbage collector is switched to manual and
    runs from a GUI-thread timer, so Qt wrappers are never finalized on a worker thread. Earlier
    designs (QThreadPool with Python `QRunnable` subclasses, then QThread workers) crashed with
    heap corruption under PySide6 6.11 on Windows.

## 22. Milestones

| Milestone | Deliverables | Done when |
|---|---|---|
| **M1 — engine + CLI + tests** | `engine` (§6–§12 except thumbnails), `cli`, fixtures generator, unit/integration/safety tests, `pyproject.toml`, `ci.yml` running tests on three OSes | `scan/classify/report/apply/undo/purge` work on the synthetic set; integrity and safety tests pass on all three OSes |
| **M2 — GUI: browser + manual mode** | `gui` skeleton, models, thumbnail cache, image/video views, settings, journal tab, i18n en/ru, themes | 100k-file synthetic folder scrolls smoothly; manual mode keys and undo work; GUI smoke tests pass |
| **M3 — group mode + auto mode in GUI** | group view, auto pipeline page with report table, sliders, examples, apply job with pause/resume/cancel, Live Photo action, purge dialog | full spec flow works end to end in the GUI on the synthetic set |
| **M4 — build, CI, Docker** | PyInstaller specs, DMG/zip/AppImage, LGPL ffmpeg sourcing, exiftool bundling, signing hooks, `release.yml`, `docker.yml`, GHCR image amd64+arm64 | tag build produces four bundles and the image; GUI opens a catalog created by the container |
| **M5 — docs + release draft** | README en/ru with generated screenshots (`QWidget.grab` on synthetic data), NAS/Docker guide, FAQ, CHANGELOG, CONTRIBUTING, templates, THIRD_PARTY_LICENSES, perf numbers, draft release v0.1.0 | draft release with artifacts exists; spec §11 checklist satisfied |

Each milestone ends with a commit and a short report (done / verified / left).

## 23. Owner decisions

1. **Name**: Framesift (decided 2026-09-25).
2. **License**: MIT, with `pi-heif` pinned and the ffmpeg HEIC fallback (§17).
3. Everything in §21 is accepted.
