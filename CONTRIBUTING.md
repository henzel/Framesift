# Contributing to Framesift

Thanks for helping. Framesift handles people's only copies of their photos, so the bar for
correctness is high and the rules below are not optional.

## Ground rules

1. **Nothing deletes files except `purge`.** No `unlink`, `rmtree` or `send2trash` anywhere
   else in the engine (a verified cross-volume move removes its source; that is the one
   exception and it lives in `fileops.copy_verify`). `tests/integration/test_safety.py` guards
   this; keep it green.
2. **File contents are never modified.** No EXIF writing, no re-encoding, no "fixing" names.
3. **Every move goes through `journal.perform_moves`** so it is recorded and undoable.
4. **The engine never imports Qt.** `framesift/engine` and `framesift/cli` must stay usable in
   Docker without PySide6; a test checks this.
5. Decisions that are not in `ARCHITECTURE.md` get written there before or with the code.

## Development setup

```bash
git clone https://github.com/henzel/PhotoSift && cd PhotoSift
python3.12 -m venv .venv && source .venv/bin/activate      # or: uv venv --python 3.12
pip install -e ".[gui,dev]"
# ffmpeg is needed for video tests; exiftool is optional
pytest -q                     # QT_QPA_PLATFORM=offscreen is set by the GUI tests themselves
ruff check framesift tests && ruff format --check framesift tests
mypy framesift/engine framesift/cli
```

Test data is synthetic: `tests/fixtures/make_dataset.py` generates the whole dataset, the
three tiny HEIC files and the HEVC clip in `tests/fixtures/bin/` were produced once by
`tests/fixtures/gen_heic_fixtures.py`. Never add real photos to the repository.

`python tests/perf/bench.py --files 50000` runs the performance check from the spec.
`python tests/fixtures/make_screenshots.py` regenerates the README screenshots offscreen.

## Pull requests

* One topic per PR; keep the diff reviewable.
* Add or extend tests for every behaviour change; category rules get a case in
  `tests/unit/test_classify.py` and, if needed, a file in the dataset generator.
* Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
  `feat(engine): …`, `fix(gui): …`, `docs: …`, `build: …`, `ci: …`, `test: …`.
* Code, comments and commit messages are in English. User-facing strings need both an English
  key and a Russian translation (`framesift/gui/i18n.py`, `framesift/engine/i18n.py`).
* CI must be green on Linux, Windows and macOS.

## Reporting bugs

Please include the Framesift version, your OS, `framesift status --json` (paths can be
redacted), the relevant lines from `<Review>/.framesift/logs/`, and whether the folders are on
a local disk or a network share. Do not attach photos.
