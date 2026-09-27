"""SQLite catalog: connection, migrations, single-writer lock and typed accessors."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from framesift import CATALOG_DIRNAME, __version__
from framesift.engine.config import ClassifyConfig, Roots
from framesift.engine.paths import is_network_path

SCHEMA_PACKAGE = "framesift.engine"
LOCK_STALE_SECONDS = 600
LOCK_HEARTBEAT_SECONDS = 30


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_op_id() -> str:
    return uuid.uuid4().hex[:16]


class CatalogError(Exception):
    pass


class LockHeld(CatalogError):
    def __init__(self, owner: dict[str, Any]):
        self.owner = owner
        super().__init__(f"catalog locked by {owner.get('host')} (pid {owner.get('pid')})")


class SchemaTooNew(CatalogError):
    pass


@dataclass(frozen=True)
class FileEntry:
    """One filesystem entry as produced by the scanner."""

    root: str
    rel_path: str
    rel_path_os: str | None
    dir_path: str
    name: str
    stem: str
    ext: str
    kind: str
    size: int
    mtime_ns: int


class CatalogLock:
    """Exclusive lock file with heartbeat; stale locks can be taken over."""

    def __init__(self, catalog_dir: Path):
        self.path = catalog_dir / "lock"
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.held = False

    def owner(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None if not self.path.exists() else {"host": "?", "pid": 0, "heartbeat": 0}

    def is_stale(self, owner: dict[str, Any]) -> bool:
        hb = owner.get("heartbeat") or 0
        if hb == 0:
            try:
                hb = self.path.stat().st_mtime
            except OSError:
                return True
        return (time.time() - float(hb)) > LOCK_STALE_SECONDS

    def acquire(self, force: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "app": f"framesift {__version__}",
            "started": time.time(),
            "heartbeat": time.time(),
        }
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                owner = self.owner() or {}
                if (
                    force
                    or self.is_stale(owner)
                    or (
                        owner.get("host") == payload["host"]
                        and not _pid_alive(int(owner.get("pid") or 0))
                    )
                ):
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                raise LockHeld(owner) from None
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            self.held = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._beat, name="catalog-lock", daemon=True)
            self._thread.start()
            return
        raise LockHeld(self.owner() or {})

    def _beat(self) -> None:
        while not self._stop.wait(LOCK_HEARTBEAT_SECONDS):
            self.touch()

    def touch(self) -> None:
        if not self.held:
            return
        try:
            data = self.owner() or {}
            data["heartbeat"] = time.time()
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def release(self) -> None:
        if not self.held:
            return
        self._stop.set()
        self.held = False
        try:
            self.path.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except Exception:  # pragma: no cover
        return True


def _is_wal(db_path: Path) -> bool:
    """True when the database file header says WAL mode (format bytes 18-19 are 2)."""
    try:
        with open(db_path, "rb") as fh:
            header = fh.read(20)
    except OSError:
        return False
    return header.startswith(b"SQLite format 3\x00") and header[18:20] == b"\x02\x02"


def _leave_wal_mode(db_path: Path) -> None:
    """Turn a WAL catalog back into a rollback-journal one without opening it in SQLite.

    0.1.2 used WAL wherever the catalog looked local, which it does inside Docker on a NAS, and
    macOS then refuses to open that database over SMB ("unable to open database file"), so
    PRAGMA journal_mode cannot convert it from there. Once no WAL content is left (a clean close
    deletes the -wal file) every page is in the main file and only the two format bytes differ.
    Called with the catalog lock held."""
    wal = db_path.with_name(db_path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise CatalogError(
            f"{db_path} has changes that were never saved to it ({wal.name}); open it once "
            "on the machine that wrote it"
        )
    with open(db_path, "r+b") as fh:
        fh.seek(18)
        fh.write(b"\x01\x01")
        fh.flush()
        os.fsync(fh.fileno())


class Catalog:
    """Thin typed layer over the SQLite catalog. One instance per process; thread-safe writes."""

    def __init__(
        self, catalog_dir: Path, conn: sqlite3.Connection, lock: CatalogLock | None, read_only: bool
    ):
        self.dir = catalog_dir
        self.path = catalog_dir / "catalog.db"
        self.conn = conn
        self.lock = lock
        self.read_only = read_only
        self._mutex = threading.RLock()

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    def open(
        cls,
        review_root: Path,
        *,
        create: bool = True,
        read_only: bool = False,
        force_lock: bool = False,
        network: bool | None = None,
    ) -> Catalog:
        catalog_dir = Path(review_root) / CATALOG_DIRNAME
        db_path = catalog_dir / "catalog.db"
        if not db_path.exists() and not create:
            raise CatalogError(f"no catalog at {db_path}")
        catalog_dir.mkdir(parents=True, exist_ok=True)
        lock: CatalogLock | None = None
        if not read_only:
            lock = CatalogLock(catalog_dir)
            lock.acquire(force=force_lock)
        try:
            if network is None:
                network = is_network_path(catalog_dir)
            if network and _is_wal(db_path):
                if read_only:
                    raise CatalogError(
                        f"{db_path} is being written in WAL mode by an older Framesift; "
                        "open it again when that job has finished"
                    )
                _leave_wal_mode(db_path)
            if read_only:
                uri = f"file:{db_path.as_posix()}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=30)
            else:
                conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA cache_size=-65536")
            conn.execute("PRAGMA temp_store=MEMORY")
            if not read_only:
                conn.execute("PRAGMA synchronous=NORMAL")
                # Never WAL, even on a local disk: the catalog is opened from other machines
                # over SMB, WAL needs memory shared by the processes of one host, and macOS
                # cannot open a WAL database on a network share at all.
                conn.execute("PRAGMA journal_mode=DELETE")
            cat = cls(catalog_dir, conn, lock, read_only)
            if not read_only:
                cat.migrate()
            else:
                cat._check_version()
            return cat
        except Exception:
            if lock:
                lock.release()
            raise

    def close(self) -> None:
        with self._mutex:
            try:
                self.conn.commit()
            except sqlite3.Error:
                pass
            self.conn.close()
        if self.lock:
            self.lock.release()

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ schema
    @staticmethod
    def _migrations() -> list[tuple[int, str]]:
        folder = resources.files(SCHEMA_PACKAGE) / "schema"
        out: list[tuple[int, str]] = []
        for entry in folder.iterdir():
            if entry.name.endswith(".sql"):
                out.append((int(entry.name.split("_", 1)[0]), entry.read_text(encoding="utf-8")))
        return sorted(out)

    def schema_version(self) -> int:
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0]) if row else 0

    def _check_version(self) -> None:
        latest = self._migrations()[-1][0]
        if self.schema_version() > latest:
            raise SchemaTooNew("catalog was created by a newer Framesift")

    def migrate(self) -> None:
        current = self.schema_version()
        migrations = self._migrations()
        latest = migrations[-1][0]
        if current > latest:
            raise SchemaTooNew("catalog was created by a newer Framesift")
        if current == latest:
            return
        if current > 0:
            backup = self.path.with_name(f"catalog.db.bak-v{current}")
            if not backup.exists():
                with sqlite3.connect(str(backup)) as dst:
                    self.conn.backup(dst)
        with self._mutex:
            for version, sql in migrations:
                if version <= current:
                    continue
                self.conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
                self.conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(version),),
                )
            if current == 0:
                self.set_meta("catalog_uuid", uuid.uuid4().hex)
                self.set_meta("created_at", utcnow())
                self.set_meta("created_by", f"framesift {__version__} ({sys.platform})")
            self.conn.commit()

    # ------------------------------------------------------------------ helpers
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._mutex:
            return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._mutex:
            self.conn.executemany(sql, rows)

    def commit(self) -> None:
        with self._mutex:
            self.conn.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._mutex:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._mutex:
            return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.one(sql, params)
        return row[0] if row else None

    # ------------------------------------------------------------------ meta / settings / hosts
    def get_meta(self, key: str) -> str | None:
        return self.scalar("SELECT value FROM meta WHERE key=?", (key,))

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    @property
    def uuid(self) -> str:
        return self.get_meta("catalog_uuid") or "unknown"

    def get_setting(self, key: str, default: Any = None) -> Any:
        raw = self.scalar("SELECT value FROM settings WHERE key=?", (key,))
        return default if raw is None else json.loads(raw)

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.commit()

    def classify_config(self) -> ClassifyConfig:
        raw = self.scalar("SELECT value FROM settings WHERE key='classify'")
        return ClassifyConfig.from_json(raw)

    def save_classify_config(self, cfg: ClassifyConfig) -> None:
        self.execute(
            "INSERT INTO settings(key,value) VALUES('classify',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (cfg.to_json(),),
        )
        self.commit()

    def record_roots(self, roots: Roots) -> None:
        if self.read_only:
            return
        self.execute(
            "INSERT INTO hosts(hostname,platform,source_path,review_path,delete_path,last_seen) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(hostname) DO UPDATE SET platform=excluded.platform, source_path=excluded.source_path, "
            "review_path=excluded.review_path, delete_path=excluded.delete_path, last_seen=excluded.last_seen",
            (
                socket.gethostname(),
                sys.platform,
                str(roots.source),
                str(roots.review),
                str(roots.delete),
                utcnow(),
            ),
        )
        self.set_meta("source_layout", json.dumps(roots.relative_layout()))
        self.set_setting("include_subfolders", roots.include_subfolders)
        self.commit()

    def resolve_roots(self, review_root: Path) -> Roots | None:
        """Roots for this host from `hosts`, else from the relative layout. None if unknown."""
        review_root = Path(review_root).resolve()
        include = bool(self.get_setting("include_subfolders", True))
        row = self.one("SELECT * FROM hosts WHERE hostname=?", (socket.gethostname(),))
        if row and Path(row["review_path"]) == review_root and Path(row["source_path"]).is_dir():
            return Roots(Path(row["source_path"]), review_root, Path(row["delete_path"]), include)
        layout_raw = self.get_meta("source_layout")
        if layout_raw:
            layout = json.loads(layout_raw)
            source = (review_root / layout["source"]).resolve()
            delete = (review_root / layout["delete"]).resolve()
            if source.is_dir():
                return Roots(source, review_root, delete, include)
        return None

    # ------------------------------------------------------------------ files
    def next_scan_generation(self) -> int:
        gen = int(self.get_meta("scan_generation") or 0) + 1
        self.set_meta("scan_generation", str(gen))
        self.commit()
        return gen

    def upsert_files(
        self, entries: Sequence[FileEntry], scan_ts: str, gen: int = 0
    ) -> tuple[int, int, int]:
        """Insert new rows, refresh changed rows, touch unchanged ones. Returns (new, changed, unchanged)."""
        if not entries:
            return (0, 0, 0)
        new = changed = unchanged = 0
        with self._mutex:
            by_key = {(e.root, e.rel_path): e for e in entries}
            existing: dict[tuple[str, str], sqlite3.Row] = {}
            keys = list(by_key)
            for i in range(0, len(keys), 400):
                chunk = keys[i : i + 400]
                placeholders = " OR ".join("(root=? AND rel_path=?)" for _ in chunk)
                params: list[Any] = []
                for root, rel in chunk:
                    params += [root, rel]
                for row in self.conn.execute(
                    f"SELECT id, root, rel_path, size, mtime_ns, status FROM files WHERE {placeholders}",
                    params,
                ):
                    existing[(row["root"], row["rel_path"])] = row
            inserts, updates, touches = [], [], []
            for key, e in by_key.items():
                row = existing.get(key)
                if row is None:
                    inserts.append(
                        (
                            e.root,
                            e.rel_path,
                            e.rel_path_os,
                            e.dir_path,
                            e.name,
                            e.stem,
                            e.ext,
                            e.kind,
                            e.size,
                            e.mtime_ns,
                            scan_ts,
                            scan_ts,
                            gen,
                        )
                    )
                    new += 1
                elif (
                    row["size"] != e.size
                    or row["mtime_ns"] != e.mtime_ns
                    or row["status"] != "present"
                ):
                    updates.append((e.size, e.mtime_ns, e.rel_path_os, scan_ts, gen, row["id"]))
                    changed += 1
                else:
                    touches.append((scan_ts, gen, e.rel_path_os, row["id"]))
                    unchanged += 1
            if inserts:
                self.conn.executemany(
                    "INSERT INTO files(root,rel_path,rel_path_os,dir_path,name,stem,ext,kind,size,mtime_ns,first_seen,last_seen,seen_gen) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    inserts,
                )
                self.conn.execute(
                    "UPDATE files SET item_id=id WHERE item_id IS NULL",
                )
            if updates:
                self.conn.executemany(
                    "UPDATE files SET size=?, mtime_ns=?, rel_path_os=?, last_seen=?, seen_gen=?, status='present', error=NULL WHERE id=?",
                    updates,
                )
            if touches:
                self.conn.executemany(
                    "UPDATE files SET last_seen=?, seen_gen=?, rel_path_os=? WHERE id=?", touches
                )
            self.conn.commit()
        return (new, changed, unchanged)

    def mark_missing(self, root: str, gen: int, failed_dirs: Sequence[str] = ()) -> int:
        """Rows of `root` not seen by scan generation `gen` become 'missing'."""
        with self._mutex:
            rows = self.conn.execute(
                "SELECT id, dir_path FROM files WHERE root=? AND status='present' AND seen_gen<?",
                (root, gen),
            ).fetchall()
            ids = []
            for row in rows:
                d = row["dir_path"]
                if any(d == f or d.startswith(f + "/") for f in failed_dirs):
                    continue
                ids.append((row["id"],))
            if ids:
                self.conn.executemany("UPDATE files SET status='missing' WHERE id=?", ids)
                self.conn.commit()
            return len(ids)

    def file(self, file_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM files WHERE id=?", (file_id,))

    def file_by_path(self, root: str, rel_path: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM files WHERE root=? AND rel_path=?", (root, rel_path))

    def files(
        self,
        root: str | None = None,
        *,
        kinds: Sequence[str] | None = None,
        status: str | None = "present",
        dir_path: str | None = None,
        recursive: bool = True,
        primaries_only: bool = False,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM files WHERE 1=1"
        params: list[Any] = []
        if root:
            sql += " AND root=?"
            params.append(root)
        if status:
            sql += " AND status=?"
            params.append(status)
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            params += list(kinds)
        if dir_path is not None:
            if recursive:
                sql += " AND (dir_path=? OR dir_path LIKE ?)"
                params += [dir_path, (dir_path + "/%") if dir_path else "%"]
            else:
                sql += " AND dir_path=?"
                params.append(dir_path)
        if primaries_only:
            sql += " AND item_id=id"
        sql += " ORDER BY rel_path"
        return self.query(sql, params)

    def item_files(self, item_id: int) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM files WHERE item_id=? ORDER BY (id=item_id) DESC, rel_path", (item_id,)
        )

    def update_file_location(
        self,
        file_id: int,
        *,
        root: str,
        rel_path: str,
        rel_path_os: str | None,
        origin_rel_path: str | None,
    ) -> None:
        from framesift.engine.paths import split_ext

        name = rel_path.rsplit("/", 1)[-1]
        dir_path = rel_path[: -len(name) - 1] if "/" in rel_path else ""
        stem, ext = split_ext(name)
        self.execute(
            "UPDATE files SET root=?, rel_path=?, rel_path_os=?, origin_rel_path=?, dir_path=?, name=?, stem=?, ext=?, "
            "status='present', last_seen=? WHERE id=?",
            (
                root,
                rel_path,
                rel_path_os,
                origin_rel_path,
                dir_path,
                name,
                stem,
                ext,
                utcnow(),
                file_id,
            ),
        )

    def set_reviewed(self, file_id: int, reviewed: bool) -> None:
        self.execute(
            "UPDATE files SET reviewed_at=? WHERE id=?", (utcnow() if reviewed else None, file_id)
        )

    def set_status(self, file_id: int, status: str, error: str | None = None) -> None:
        self.execute("UPDATE files SET status=?, error=? WHERE id=?", (status, error, file_id))

    # ------------------------------------------------------------------ meta
    def files_needing_meta(self, root: str | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT f.* FROM files f LEFT JOIN file_meta m ON m.file_id=f.id "
            "WHERE f.status='present' AND f.kind IN ('photo','raw','video') "
            "AND (m.file_id IS NULL OR m.size<>f.size OR m.mtime_ns<>f.mtime_ns)"
        )
        params: list[Any] = []
        if root:
            sql += " AND f.root=?"
            params.append(root)
        return self.query(sql + " ORDER BY f.id", params)

    def set_file_meta(self, file_id: int, size: int, mtime_ns: int, fields: dict[str, Any]) -> None:
        cols = [
            "format",
            "width",
            "height",
            "orientation",
            "make",
            "model",
            "software",
            "date_taken",
            "date_taken_ts",
            "date_source",
            "user_comment",
            "content_id",
            "duration_ms",
            "codec",
            "bitrate",
            "fps",
            "has_preview",
            "preview_kind",
            "preview_offset",
            "preview_length",
            "camera_known",
            "structure_ok",
            "extractor",
        ]
        values = [fields.get(c) for c in cols]
        values[cols.index("camera_known")] = int(bool(fields.get("camera_known")))
        values[cols.index("extractor")] = fields.get("extractor") or "none"
        self.execute(
            "INSERT OR REPLACE INTO file_meta(file_id,size,mtime_ns,%s,extracted_at) VALUES(?,?,?,%s,?)"
            % (",".join(cols), ",".join("?" * len(cols))),
            [file_id, size, mtime_ns, *values, utcnow()],
        )

    def meta(self, file_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM file_meta WHERE file_id=?", (file_id,))

    def files_with_meta(self, root: str = "source", status: str = "present") -> list[sqlite3.Row]:
        """Files joined with metadata and analysis (columns prefixed m_ / a_)."""
        return self.query(
            "SELECT f.*, "
            "m.format AS m_format, m.width AS m_width, m.height AS m_height, m.orientation AS m_orientation, "
            "m.make AS m_make, m.model AS m_model, m.software AS m_software, m.date_taken AS m_date_taken, "
            "m.date_taken_ts AS m_date_taken_ts, m.date_source AS m_date_source, m.user_comment AS m_user_comment, "
            "m.content_id AS m_content_id, m.duration_ms AS m_duration_ms, m.codec AS m_codec, m.bitrate AS m_bitrate, "
            "m.fps AS m_fps, m.has_preview AS m_has_preview, m.preview_kind AS m_preview_kind, "
            "m.preview_offset AS m_preview_offset, m.preview_length AS m_preview_length, "
            "m.camera_known AS m_camera_known, m.structure_ok AS m_structure_ok, m.extractor AS m_extractor, "
            "a.partial_hash AS a_partial_hash, a.full_hash AS a_full_hash, a.phash AS a_phash, "
            "a.sharpness AS a_sharpness, a.brightness AS a_brightness, a.dark_p99 AS a_dark_p99, "
            "a.decode_error AS a_decode_error, "
            "(a.file_id IS NOT NULL AND a.size=f.size AND a.mtime_ns=f.mtime_ns) AS a_valid "
            "FROM files f LEFT JOIN file_meta m ON m.file_id=f.id AND m.size=f.size AND m.mtime_ns=f.mtime_ns "
            "LEFT JOIN file_analysis a ON a.file_id=f.id "
            "WHERE f.root=? AND f.status=? ORDER BY f.id",
            (root, status),
        )

    # ------------------------------------------------------------------ analysis
    def set_analysis(self, file_id: int, size: int, mtime_ns: int, **fields: Any) -> None:
        allowed = {
            "partial_hash",
            "full_hash",
            "phash",
            "phash_source",
            "sharpness",
            "brightness",
            "dark_p99",
            "decode_error",
        }
        with self._mutex:
            row = self.conn.execute(
                "SELECT size, mtime_ns FROM file_analysis WHERE file_id=?", (file_id,)
            ).fetchone()
            if row is None or row["size"] != size or row["mtime_ns"] != mtime_ns:
                self.conn.execute(
                    "INSERT OR REPLACE INTO file_analysis(file_id,size,mtime_ns,analyzed_at) VALUES(?,?,?,?)",
                    (file_id, size, mtime_ns, utcnow()),
                )
            sets = [f"{k}=?" for k in fields if k in allowed]
            if sets:
                self.conn.execute(
                    f"UPDATE file_analysis SET {', '.join(sets)}, analyzed_at=? WHERE file_id=?",
                    [*[v for k, v in fields.items() if k in allowed], utcnow(), file_id],
                )

    def analysis(self, file_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM file_analysis WHERE file_id=?", (file_id,))

    # ------------------------------------------------------------------ sessions / jobs
    def start_session(self, kind: str, app: str, params: dict[str, Any] | None = None) -> int:
        cur = self.execute(
            "INSERT INTO sessions(kind,host,app,started_at,params) VALUES(?,?,?,?,?)",
            (kind, socket.gethostname(), app, utcnow(), json.dumps(params or {})),
        )
        self.commit()
        return int(cur.lastrowid or 0)

    def end_session(self, session_id: int) -> None:
        self.execute("UPDATE sessions SET ended_at=? WHERE id=?", (utcnow(), session_id))
        self.commit()

    def create_job(
        self, kind: str, session_id: int | None, params: dict[str, Any] | None = None
    ) -> int:
        now = utcnow()
        cur = self.execute(
            "INSERT INTO jobs(session_id,kind,state,params,progress,started_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (session_id, kind, "running", json.dumps(params or {}), "{}", now, now),
        )
        self.commit()
        return int(cur.lastrowid or 0)

    def update_job(
        self,
        job_id: int,
        *,
        state: str | None = None,
        progress: dict[str, Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        sets: list[str] = ["updated_at=?"]
        params: list[Any] = [utcnow()]
        if state:
            sets.append("state=?")
            params.append(state)
            if state in ("done", "cancelled", "failed"):
                sets.append("finished_at=?")
                params.append(utcnow())
        if progress is not None:
            sets.append("progress=?")
            params.append(json.dumps(progress))
        if checkpoint is not None:
            sets.append("checkpoint=?")
            params.append(json.dumps(checkpoint))
        if error is not None:
            sets.append("error=?")
            params.append(error)
        params.append(job_id)
        self.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", params)
        self.commit()

    def job(self, job_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def jobs(
        self, kind: str | None = None, states: Sequence[str] | None = None, limit: int = 20
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if states:
            sql += " AND state IN (%s)" % ",".join("?" * len(states))
            params += list(states)
        return self.query(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])

    # ------------------------------------------------------------------ candidates
    def replace_candidates(self, run_id: int, rows: Iterable[dict[str, Any]]) -> int:
        items = list(rows)
        with self._mutex:
            self.conn.execute("DELETE FROM candidates WHERE applied_op IS NULL")
            self.conn.executemany(
                "INSERT INTO candidates(run_id,file_id,category,confidence,reason_key,reason_params,group_id,is_keeper,also) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        run_id,
                        r["file_id"],
                        r["category"],
                        r["confidence"],
                        r["reason_key"],
                        json.dumps(r.get("reason_params") or {}),
                        r.get("group_id"),
                        int(bool(r.get("is_keeper"))),
                        json.dumps(r.get("also") or []),
                    )
                    for r in items
                ],
            )
            self.set_meta("last_classify_run", str(run_id))
            self.conn.commit()
            return len(items)

    def latest_run_id(self) -> int | None:
        raw = self.get_meta("last_classify_run")
        return int(raw) if raw else None

    def candidates(
        self,
        run_id: int | None = None,
        category: str | None = None,
        *,
        unapplied_only: bool = False,
        include_keepers: bool = True,
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT c.*, f.root AS f_root, f.rel_path AS f_rel_path, f.rel_path_os AS f_rel_path_os, "
            "f.size AS f_size, f.item_id AS f_item_id, f.status AS f_status, f.name AS f_name, f.kind AS f_kind "
            "FROM candidates c JOIN files f ON f.id=c.file_id WHERE 1=1"
        )
        params: list[Any] = []
        if run_id is not None:
            sql += " AND c.run_id=?"
            params.append(run_id)
        if category:
            sql += " AND c.category=?"
            params.append(category)
        if unapplied_only:
            sql += " AND c.applied_op IS NULL"
        if not include_keepers:
            sql += " AND c.is_keeper=0"
        return self.query(sql + " ORDER BY c.category, c.group_id, c.is_keeper DESC, c.id", params)

    def mark_candidate_applied(self, candidate_id: int, op_id: str) -> None:
        self.execute("UPDATE candidates SET applied_op=? WHERE id=?", (op_id, candidate_id))

    # ------------------------------------------------------------------ journal
    def add_journal(self, rows: Sequence[dict[str, Any]]) -> list[int]:
        ids: list[int] = []
        with self._mutex:
            for r in rows:
                cur = self.conn.execute(
                    "INSERT INTO journal(op_id,session_id,ts,action,file_id,item_id,from_root,from_rel,to_root,to_rel,"
                    "method,conflict,category,reason_key,reason_params,size,undoes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        r["op_id"],
                        r["session_id"],
                        r.get("ts") or utcnow(),
                        r["action"],
                        r["file_id"],
                        r["item_id"],
                        r.get("from_root"),
                        r.get("from_rel"),
                        r.get("to_root"),
                        r.get("to_rel"),
                        r.get("method"),
                        int(bool(r.get("conflict"))),
                        r.get("category"),
                        r.get("reason_key"),
                        json.dumps(r["reason_params"])
                        if r.get("reason_params") is not None
                        else None,
                        int(r.get("size") or 0),
                        r.get("undoes"),
                    ),
                )
                ids.append(int(cur.lastrowid or 0))
        return ids

    def journal(
        self,
        *,
        session_id: int | None = None,
        category: str | None = None,
        action: str | None = None,
        text: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM journal WHERE 1=1"
        params: list[Any] = []
        if session_id is not None:
            sql += " AND session_id=?"
            params.append(session_id)
        if category:
            sql += " AND category=?"
            params.append(category)
        if action:
            sql += " AND action=?"
            params.append(action)
        if text:
            sql += " AND (from_rel LIKE ? OR to_rel LIKE ?)"
            params += [f"%{text}%", f"%{text}%"]
        return self.query(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", [*params, limit, offset])

    def op_rows(self, op_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM journal WHERE op_id=? ORDER BY id", (op_id,))

    def undoable_ops(
        self,
        *,
        session_id: int | None = None,
        category: str | None = None,
        limit: int | None = None,
    ) -> list[str]:
        """Op ids (newest first) that still have not-undone, undoable rows."""
        sql = (
            "SELECT op_id, MAX(id) AS last_id FROM journal WHERE undone_by IS NULL "
            "AND action NOT IN ('undo','purge')"
        )
        params: list[Any] = []
        if session_id is not None:
            sql += " AND session_id=?"
            params.append(session_id)
        if category:
            sql += " AND category=?"
            params.append(category)
        sql += " GROUP BY op_id ORDER BY last_id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [r["op_id"] for r in self.query(sql, params)]

    def set_undone(self, row_id: int, by_id: int) -> None:
        self.execute("UPDATE journal SET undone_by=? WHERE id=?", (by_id, row_id))

    def session_counters(self, session_id: int) -> dict[str, Any]:
        rows = self.query(
            "SELECT j.action, COUNT(DISTINCT j.op_id) AS ops, SUM(j.size) AS bytes FROM journal j "
            "JOIN files f ON f.id=j.file_id WHERE j.session_id=? AND j.undone_by IS NULL "
            "AND (j.action<>'to_delete' OR (f.root='delete' AND f.rel_path=j.to_rel)) GROUP BY j.action",
            (session_id,),
        )
        out = {"kept": 0, "to_delete": 0, "bytes_to_delete": 0, "restored": 0}
        for r in rows:
            if r["action"] == "keep":
                out["kept"] += r["ops"]
            elif r["action"] == "to_delete":
                out["to_delete"] += r["ops"]
                out["bytes_to_delete"] += int(r["bytes"] or 0)
            elif r["action"] == "restore":
                out["restored"] += r["ops"]
        return out

    # ------------------------------------------------------------------ issues / stats
    def add_issue(self, job_id: int | None, path: str | None, kind: str, message: str) -> None:
        self.execute(
            "INSERT INTO issues(job_id,ts,path,kind,message) VALUES(?,?,?,?,?)",
            (job_id, utcnow(), path, kind, message[:2000]),
        )

    def issues(self, job_id: int | None = None, limit: int = 1000) -> list[sqlite3.Row]:
        if job_id is None:
            return self.query("SELECT * FROM issues ORDER BY id DESC LIMIT ?", (limit,))
        return self.query(
            "SELECT * FROM issues WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)
        )

    def root_stats(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self.query(
            "SELECT root, COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM files WHERE status='present' GROUP BY root"
        ):
            out[r["root"]] = {"files": r["n"], "bytes": r["bytes"]}
        return out

    def review_category_stats(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT CASE WHEN instr(rel_path,'/')>0 THEN substr(rel_path,1,instr(rel_path,'/')-1) ELSE '' END AS cat, "
            "COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM files WHERE root='review' AND status='present' GROUP BY cat"
        )
        return [{"category": r["cat"], "files": r["n"], "bytes": r["bytes"]} for r in rows]

    def iter_ids(self, sql: str, params: Sequence[Any] = ()) -> Iterator[int]:
        with self._mutex:
            cur = self.conn.execute(sql, params)
            rows = cur.fetchall()
        for r in rows:
            yield int(r[0])
