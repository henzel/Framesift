from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from framesift.engine.catalog import Catalog, CatalogError, CatalogLock, LockHeld, SchemaTooNew
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


def journal_mode(cat: Catalog) -> str:
    return cat.conn.execute("PRAGMA journal_mode").fetchone()[0]


def test_catalog_never_uses_wal(tmp_path: Path) -> None:
    """The catalog is opened from other machines over SMB; WAL only works on one host."""
    cat = Catalog.open(tmp_path / "r", network=False)
    assert journal_mode(cat) == "delete"
    cat.close()


def connect_like_macos_over_smb(
    real_connect: Callable[..., sqlite3.Connection],
) -> Callable[..., sqlite3.Connection]:
    """sqlite3.connect as on macOS for a file on an SMB share: SQLite locks such files without
    shared memory, like the unix-dotfile VFS, and cannot open a WAL database there."""

    def connect(
        database: str | Path, *args: Any, uri: bool = False, **kwargs: Any
    ) -> sqlite3.Connection:
        if uri:
            database = f"{database}{'&' if '?' in str(database) else '?'}vfs=unix-dotfile"
        else:
            database = f"file:{Path(database).as_posix()}?vfs=unix-dotfile"
        return real_connect(database, *args, uri=True, **kwargs)

    return connect


def test_wal_catalog_from_an_older_nas_opens_over_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cat = Catalog.open(tmp_path / "r")
    cat.set_setting("include_subfolders", False)
    cat.close()
    db = tmp_path / "r" / ".framesift" / "catalog.db"
    wal = sqlite3.connect(db)  # what 0.1.2 did inside Docker on a NAS, where paths look local
    assert wal.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    wal.close()
    assert db.read_bytes()[18:20] == b"\x02\x02"
    if sys.platform != "win32":  # no unix-dotfile VFS on Windows
        monkeypatch.setattr(sqlite3, "connect", connect_like_macos_over_smb(sqlite3.connect))
    again = Catalog.open(tmp_path / "r", network=True)  # was "unable to open database file"
    assert journal_mode(again) == "delete"
    assert again.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert again.get_setting("include_subfolders") is False
    again.close()
    assert db.read_bytes()[18:20] == b"\x01\x01"


def test_wal_catalog_with_unsaved_changes_is_left_alone(tmp_path: Path) -> None:
    Catalog.open(tmp_path / "r").close()
    db = tmp_path / "r" / ".framesift" / "catalog.db"
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO meta(key, value) VALUES('probe', '1')")
    writer.commit()  # stays in the -wal file while this connection is open
    try:
        with pytest.raises(CatalogError, match="never saved"):
            Catalog.open(tmp_path / "r", network=True)
        assert db.read_bytes()[18:20] == b"\x02\x02"
        assert not (tmp_path / "r" / ".framesift" / "lock").exists()
    finally:
        writer.close()
    local = Catalog.open(tmp_path / "r", network=False)  # on a local disk SQLite converts it
    assert journal_mode(local) == "delete" and local.get_meta("probe") == "1"
    local.close()


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
