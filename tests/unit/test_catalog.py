from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from framesift.engine.catalog import Catalog, CatalogLock, LockHeld, SchemaTooNew
from framesift.engine.config import ClassifyConfig, Roots


def test_open_migrate_and_reopen(tmp_path: Path) -> None:
    review = tmp_path / "review"
    cat = Catalog.open(review)
    assert cat.schema_version() == 1 and len(cat.uuid) == 32
    uuid = cat.uuid
    cat.set_setting("include_subfolders", False)
    cat.close()
    assert not (review / ".framesift" / "lock").exists()
    again = Catalog.open(review, create=False)
    assert again.uuid == uuid and again.get_setting("include_subfolders") is False
    again.close()
    ro = Catalog.open(review, read_only=True)
    assert ro.get_setting("include_subfolders") is False and ro.lock is None
    ro.close()


def test_schema_too_new_is_refused(tmp_path: Path) -> None:
    cat = Catalog.open(tmp_path / "r")
    cat.set_meta("schema_version", "999")
    cat.commit()
    cat.close()
    with pytest.raises(SchemaTooNew):
        Catalog.open(tmp_path / "r")
    assert not (tmp_path / "r" / ".framesift" / "lock").exists()


def test_lock_is_exclusive_and_stale_locks_are_taken_over(tmp_path: Path) -> None:
    review = tmp_path / "review"
    cat = Catalog.open(review)
    with pytest.raises(LockHeld) as exc:
        Catalog.open(review)
    assert exc.value.owner["pid"] == os.getpid()
    cat.close()
    lock_path = review / ".framesift" / "lock"
    lock_path.write_text(
        json.dumps({"host": "nas", "pid": 424242, "heartbeat": time.time() - 100000})
    )
    cat = Catalog.open(review)  # stale → taken over
    assert json.loads(lock_path.read_text())["pid"] == os.getpid()
    cat.close()
    lock_path.write_text(json.dumps({"host": "nas", "pid": 424242, "heartbeat": time.time()}))
    with pytest.raises(LockHeld):
        Catalog.open(review)
    cat = Catalog.open(review, force_lock=True)
    cat.close()
    lock = CatalogLock(review / ".framesift")
    lock.acquire()
    lock.touch()
    assert json.loads(lock.path.read_text())["heartbeat"] > 0
    lock.release()
    assert not lock.path.exists()


def test_roots_resolution_across_hosts(tmp_path: Path) -> None:
    source = tmp_path / "archive"
    source.mkdir()
    roots = Roots.for_source(source)
    cat = Catalog.open(roots.review)
    cat.record_roots(roots)
    assert cat.resolve_roots(roots.review) == roots
    # another machine: no hosts row for it, relative layout still resolves
    cat.execute("DELETE FROM hosts")
    cat.commit()
    resolved = cat.resolve_roots(roots.review)
    assert resolved is not None and resolved.source == source and resolved.delete == roots.delete
    layout = json.loads(cat.get_meta("source_layout"))
    assert layout == {"source": "..", "delete": "../_framesift_delete"}
    cat.close()


def test_classify_config_roundtrip(tmp_path: Path) -> None:
    cat = Catalog.open(tmp_path / "r")
    cfg = ClassifyConfig(short_video_seconds=4.5, move_all_aae=True)
    cfg.enabled["similar"] = False
    cat.save_classify_config(cfg)
    loaded = cat.classify_config()
    assert (
        loaded.short_video_seconds == 4.5
        and loaded.move_all_aae
        and not loaded.is_enabled("similar")
    )
    assert loaded.is_enabled("junk")
    cat.close()
    assert ClassifyConfig.from_json('{"unknown": 1}').blur_threshold == 12.0
