## Summary

<!-- What changes and why. Link the issue if there is one. -->

## Safety checklist

- [ ] No new code path deletes files (only `engine/purge.py` may)
- [ ] File contents and metadata are never modified
- [ ] Every new move goes through `journal.perform_moves` and is undoable
- [ ] `framesift/engine` and `framesift/cli` still import without Qt

## Tests

- [ ] Added or updated tests
- [ ] `pytest`, `ruff`, `mypy framesift/engine framesift/cli` pass locally
- [ ] `ARCHITECTURE.md` updated if a design decision changed
