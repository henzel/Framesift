from __future__ import annotations

from pathlib import Path

from helpers import content_multiset, snapshot

from framesift.engine import actions, api
from framesift.engine.config import ClassifyConfig
from framesift.engine.journal import undo_ops


def _prepare(root: Path) -> api.Workspace:
    ws = api.open_workspace(root)
    api.run_scan(ws)
    api.run_classify(ws, ClassifyConfig(), workers=1)
    return ws


def test_dry_run_never_touches_the_filesystem(fresh) -> None:
    root, _ = fresh
    before = snapshot(root)
    ws = _prepare(root)
    try:
        stats = api.run_apply(ws, dry_run=True)
        assert stats.items > 0 and stats.dry_run
        live = api.run_live_photos(ws, dry_run=True)
        assert live.dry_run
        assert snapshot(root) == before
        undo = api.run_undo(ws, "all", dry_run=True)
        assert undo.dry_run and undo.ops == 0
        assert snapshot(root) == before
        assert ws.catalog.journal() == []
    finally:
        ws.close()


def test_apply_all_then_undo_all_restores_tree_exactly(fresh) -> None:
    root, manifest = fresh
    before = snapshot(root)
    contents = content_multiset(root)
    ws = _prepare(root)
    try:
        stats = api.run_apply(ws, dry_run=False)
        assert stats.items >= 15 and not stats.errors
        live = api.run_live_photos(ws, dry_run=False)
        assert live.files == len(manifest["live_pairs"])
        review = root / "_framesift_review"
        assert (review / "duplicates" / "2020" / "IMG_0004 (1).JPG").exists()
        assert (review / "junk" / "junk" / ".DS_Store").exists()
        assert not (root / "2020" / "IMG_0004 (1).JPG").exists()
        assert content_multiset(root) == contents, "no content may disappear before purge"
        if manifest["live_pairs"]:
            assert (review / "live_photos" / "2020" / "IMG_0001.MOV").exists()
        undo = api.run_undo(ws, "all", dry_run=False)
        assert undo.ops == stats.items + live.files and not undo.errors
        assert snapshot(root) == before
        # the live video is a companion again
        if manifest["live_pairs"]:
            mov = ws.catalog.file_by_path("source", "2020/IMG_0001.MOV")
            photo = ws.catalog.file_by_path("source", "2020/IMG_0001.JPG")
            assert mov["item_id"] == photo["id"] and mov["detached_at"] is None
        assert api.run_undo(ws, "all", dry_run=False).ops == 0
    finally:
        ws.close()


def test_undo_by_category_and_session(fresh) -> None:
    root, _ = fresh
    before = snapshot(root)
    ws = _prepare(root)
    try:
        api.run_apply(ws, dry_run=False, categories=["junk", "screenshots"])
        undone = api.run_undo(ws, "category", category="junk", dry_run=False)
        assert undone.ops == 6
        assert (root / "junk" / ".DS_Store").exists()
        assert not (root / "2020" / "IMG_0005.PNG").exists()
        undone = api.run_undo(ws, "session", dry_run=False)
        assert undone.ops == 3
        assert snapshot(root) == before
    finally:
        ws.close()


def test_manual_actions_follow_the_table(fresh) -> None:
    root, _ = fresh
    ws = _prepare(root)
    cat, roots = ws.catalog, ws.roots
    try:
        sid = ws.session_id
        photo = cat.file_by_path("source", "2020/IMG_0003.JPG")
        item = photo["id"]
        # Source + keep → reviewed, no move
        actions.keep(cat, roots, item, sid)
        assert cat.file(item)["reviewed_at"] and (root / "2020" / "IMG_0003.JPG").exists()
        # undo keep → not reviewed
        assert actions.undo_last(cat, roots, sid)
        assert cat.file(item)["reviewed_at"] is None
        # Source + to delete → <Delete>/<rel path>, companions follow (.AAE)
        actions.to_delete(cat, roots, item, sid)
        assert (root / "_framesift_delete" / "2020" / "IMG_0003.JPG").exists()
        assert (root / "_framesift_delete" / "2020" / "IMG_0003.AAE").exists()
        assert (
            cat.file(item)["root"] == "delete"
            and cat.file(item)["origin_rel_path"] == "2020/IMG_0003.JPG"
        )
        # Delete + to delete → nothing
        assert actions.to_delete(cat, roots, item, sid) is None
        # Delete + keep → back to source
        actions.keep(cat, roots, item, sid)
        assert (root / "2020" / "IMG_0003.JPG").exists() and cat.file(item)["root"] == "source"
        assert cat.file(item)["origin_rel_path"] is None
        # Review + keep → back home and reviewed
        api.run_apply(ws, dry_run=False, categories=["screenshots"])
        shot = cat.file_by_path("review", "screenshots/2020/IMG_0007.PNG")
        actions.keep(cat, roots, shot["id"], sid)
        row = cat.file(shot["id"])
        assert (
            row["root"] == "source"
            and row["rel_path"] == "2020/IMG_0007.PNG"
            and row["reviewed_at"]
        )
        # Review + to delete → <Delete>/<origin path>
        shot2 = cat.file_by_path("review", "screenshots/2020/IMG_0005.PNG")
        actions.to_delete(cat, roots, shot2["id"], sid)
        assert (root / "_framesift_delete" / "2020" / "IMG_0005.PNG").exists()
        counters = cat.session_counters(sid)
        assert counters["to_delete"] == 1 and counters["bytes_to_delete"] > 0
        # multi-level undo walks back through the session
        assert actions.undo_last(cat, roots, sid)
        assert cat.file(shot2["id"])["root"] == "review"
        assert actions.undo_last(cat, roots, sid)
        assert (
            cat.file(shot["id"])["root"] == "review" and cat.file(shot["id"])["reviewed_at"] is None
        )
    finally:
        ws.close()


def test_name_conflicts_get_suffix_and_journal_flag(fresh) -> None:
    root, _ = fresh
    ws = _prepare(root)
    cat, roots = ws.catalog, ws.roots
    try:
        sid = ws.session_id
        photo = cat.file_by_path("source", "2020/IMG_0004.JPG")
        actions.to_delete(cat, roots, photo["id"], sid)
        # something else appears at the original place
        (root / "2020" / "IMG_0004.JPG").write_bytes(b"\xff\xd8intruder\xff\xd9")
        outcome = actions.restore(cat, roots, photo["id"], sid)
        assert outcome.conflicts == 1
        row = cat.file(photo["id"])
        assert row["rel_path"] == "2020/IMG_0004~2.JPG"
        assert (root / "2020" / "IMG_0004~2.JPG").exists()
        assert (root / "2020" / "IMG_0004.JPG").read_bytes().endswith(b"intruder\xff\xd9")
        journal = cat.journal(action="restore")
        assert journal and journal[0]["conflict"] == 1
        issues = [i for i in cat.issues() if i["kind"] == "name_conflict"]
        assert issues
    finally:
        ws.close()


def test_undo_skips_files_moved_since(fresh) -> None:
    root, _ = fresh
    ws = _prepare(root)
    cat, roots = ws.catalog, ws.roots
    try:
        sid = ws.session_id
        api.run_apply(ws, dry_run=False, categories=["duplicates"])
        moved = root / "_framesift_review" / "duplicates" / "2020" / "IMG_0004 (1).JPG"
        assert moved.exists()
        moved.rename(moved.with_name("elsewhere.JPG"))
        ops = cat.undoable_ops()
        stats = undo_ops(cat, roots, ops, session_id=sid)
        assert stats.skipped == 1 and stats.files == 0 and stats.errors
    finally:
        ws.close()
