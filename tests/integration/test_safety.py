"""Hard rules: nothing but purge deletes; the engine never imports Qt; the catalog is the only side effect."""

from __future__ import annotations

import subprocess
import sys

from conftest import content_multiset, snapshot

from framesift.engine import api
from framesift.engine.config import ClassifyConfig


def test_engine_does_not_import_qt() -> None:
    code = (
        "import sys, framesift.engine.api, framesift.engine.classify, framesift.engine.scanner, "
        "framesift.engine.imaging, framesift.cli.main; "
        "print(sorted(m for m in sys.modules if m.startswith(('PySide', 'PyQt', 'shiboken'))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "[]"


def test_only_purge_deletes_content(fresh) -> None:
    root, _ = fresh
    contents = content_multiset(root)
    ws = api.open_workspace(root)
    try:
        api.run_scan(ws)
        api.run_classify(ws, ClassifyConfig(), workers=1)
        api.run_apply(ws, dry_run=False)
        api.run_live_photos(ws, dry_run=False)
        assert content_multiset(root) == contents
        api.run_undo(ws, "category", category="junk", dry_run=False)
        assert content_multiset(root) == contents
        # move a couple of items to Delete through the manual actions
        from framesift.engine import actions

        sid = ws.session_id
        for rel in ("2020/IMG_0004.JPG", "messenger/photo_no_camera.jpg"):
            row = ws.catalog.file_by_path("source", rel) or ws.catalog.file_by_path(
                "review", "no_camera_media/" + rel
            )
            actions.to_delete(ws.catalog, ws.roots, row["item_id"], sid)
        assert content_multiset(root) == contents
        preview = api.status(ws)["purge"]
        assert preview["files"] == 2
        dry = api.run_purge(ws, dry_run=True, use_trash=False)
        assert dry.files == 2 and content_multiset(root) == contents
        real = api.run_purge(ws, dry_run=False, use_trash=False)
        assert real.files == 2 and not real.errors
        after = content_multiset(root)
        assert sum(after.values()) == sum(contents.values()) - 2
        assert not any((root / "_framesift_delete").rglob("*.jpg")) and not any(
            (root / "_framesift_delete").rglob("*.JPG")
        )
        assert [r["action"] for r in ws.catalog.journal(action="purge")] == ["purge", "purge"]
        assert ws.catalog.undoable_ops(
            category=None
        )  # earlier moves stay undoable, purge rows do not
        assert all(
            r["action"] != "purge"
            for op in ws.catalog.undoable_ops()
            for r in ws.catalog.op_rows(op)
        )
    finally:
        ws.close()


def test_scan_and_classify_leave_files_untouched(fresh) -> None:
    root, _ = fresh
    before = snapshot(root)
    ws = api.open_workspace(root)
    try:
        api.run_scan(ws)
        api.run_classify(ws, ClassifyConfig(), workers=1)
        api.get_plan(ws)
        assert snapshot(root) == before
        assert (root / "_framesift_review" / ".framesift" / "catalog.db").exists()
        assert not (root / "_framesift_delete").exists() or not any(
            (root / "_framesift_delete").iterdir()
        )
    finally:
        ws.close()
