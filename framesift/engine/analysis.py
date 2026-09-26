"""Pixel and content analysis: pHash, sharpness, darkness, hashes. Runs in worker processes."""

from __future__ import annotations

import gc
import math
import os
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from framesift.engine.catalog import Catalog
from framesift.engine.config import Roots
from framesift.engine.paths import os_path

SMALL_SIDE = 256
HASH_SIDE = 32
LOW_FREQ = 8


def _dct_matrix(n: int):
    import numpy as np

    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(math.pi * (2 * i + 1) * k / (2 * n)) * math.sqrt(2.0 / n)
    m[0, :] *= 1 / math.sqrt(2.0)
    return m


_DCT: Any = None


def phash_from_gray(gray) -> int:
    """64-bit perceptual hash (imagehash-compatible algorithm) of a 32×32 float array."""
    global _DCT
    import numpy as np

    if _DCT is None:
        _DCT = _dct_matrix(HASH_SIDE)
    arr = np.asarray(gray, dtype=np.float64)
    dct = _DCT @ arr @ _DCT.T
    low = dct[:LOW_FREQ, :LOW_FREQ]
    med = np.median(low)
    bits = (low > med).flatten()
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return value


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def sharpness_of(gray) -> float:
    """Variance of the 3×3 Laplacian on a 0..255 grayscale array."""
    import numpy as np

    g = np.asarray(gray, dtype=np.float32)
    if g.shape[0] < 3 or g.shape[1] < 3:
        return 0.0
    lap = -4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]
    return float(lap.var())


def brightness_of(gray) -> tuple[float, float]:
    import numpy as np

    g = np.asarray(gray, dtype=np.float32) / 255.0
    return float(g.mean()), float(np.percentile(g, 99))


def analyze_pixels(path: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Compute phash / sharpness / brightness for one photo. Never raises; errors go to decode_error."""
    from framesift.engine.imaging import DecodeError, small_image

    try:
        im, source = small_image(path, meta, SMALL_SIDE)
    except DecodeError as exc:
        return {"decode_error": str(exc)[:300]}
    except Exception as exc:  # pragma: no cover - defensive
        return {"decode_error": f"{type(exc).__name__}: {exc}"[:300]}
    import numpy as np

    gray = im.convert("L")
    g = np.asarray(gray, dtype=np.float32)
    mean, p99 = brightness_of(g)
    sharp = sharpness_of(g)
    hash_im = gray.resize(
        (HASH_SIDE, HASH_SIDE), resample=1
    )  # LANCZOS-free: bilinear (1) is enough
    ph = phash_from_gray(np.asarray(hash_im, dtype=np.float64))
    return {
        "phash": ph - (1 << 64) if ph >= (1 << 63) else ph,  # keep within SQLite INTEGER range
        "phash_source": source,
        "sharpness": round(sharp, 3),
        "brightness": round(mean, 4),
        "dark_p99": round(p99, 4),
        "decode_error": None,
    }


def phash_unsigned(value: int | None) -> int | None:
    if value is None:
        return None
    return value + (1 << 64) if value < 0 else value


def _worker(task: tuple[str, str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    kind, path, meta = task
    if kind == "partial":
        from framesift.engine.hashing import partial_hash

        try:
            return path, {"partial_hash": partial_hash(Path(path), meta.get("size"))}
        except OSError as exc:
            return path, {"error": str(exc)}
    if kind == "full":
        from framesift.engine.hashing import full_hash

        try:
            return path, {"full_hash": full_hash(Path(path))}
        except OSError as exc:
            return path, {"error": str(exc)}
    return path, analyze_pixels(Path(path), meta)


def set_low_priority() -> None:
    """Lower CPU and I/O priority of the current process (nice/ionice or Windows classes)."""
    try:
        import psutil

        p = psutil.Process()
        if os.name == "nt":  # pragma: no cover
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            try:
                p.ionice(psutil.IOPRIO_LOW)
            except Exception:
                pass
        else:
            try:
                p.nice(15)
            except Exception:
                pass
            try:
                p.ionice(psutil.IOPRIO_CLASS_IDLE)
            except Exception:
                try:
                    p.ionice(psutil.IOPRIO_CLASS_BE, value=7)
                except Exception:
                    pass
    except Exception:  # pragma: no cover
        pass


def _init_worker(low_priority: bool) -> None:
    # A forked worker inherits the parent's GC state; the GUI switches automatic collection
    # off (it collects on its GUI thread), the worker must not.
    gc.enable()
    if low_priority:
        set_low_priority()


def run_tasks(
    catalog: Catalog,
    roots: Roots,
    tasks: Sequence[tuple[str, dict[str, Any]]],
    *,
    workers: int | None = None,
    low_priority: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancel: threading.Event | None = None,
    stage: str = "analyze",
) -> int:
    """Run (kind, row) tasks in a process pool and store the results. Returns the number done."""
    if not tasks:
        return 0
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    payload = []
    by_path: dict[str, dict[str, Any]] = {}
    for kind, row in tasks:
        path = str(os_path(roots.path(row["root"]), row["rel_path"], row.get("rel_path_os")))
        meta = {
            "size": row["size"],
            "format": row.get("m_format"),
            "ext": row.get("ext"),
            "orientation": row.get("m_orientation"),
            "preview_kind": row.get("m_preview_kind"),
            "preview_offset": row.get("m_preview_offset"),
            "preview_length": row.get("m_preview_length"),
        }
        payload.append((kind, path, meta))
        by_path[path] = row
    done = 0
    total = len(payload)
    if workers == 1 or total <= 4:
        results = map(_worker, payload)
        for path, result in results:
            _store(catalog, by_path[path], result)
            done += 1
            if progress and done % 50 == 0:
                progress({"stage": stage, "done": done, "total": total})
            if cancel is not None and cancel.is_set():
                break
    else:
        with ProcessPoolExecutor(
            max_workers=workers, initializer=_init_worker, initargs=(low_priority,)
        ) as pool:
            for path, result in pool.map(_worker, payload, chunksize=8):
                _store(catalog, by_path[path], result)
                done += 1
                if progress and done % 50 == 0:
                    progress({"stage": stage, "done": done, "total": total})
                if cancel is not None and cancel.is_set():
                    pool.shutdown(cancel_futures=True)
                    break
    catalog.commit()
    if progress:
        progress({"stage": stage, "done": done, "total": total})
    return done


def _store(catalog: Catalog, row: dict[str, Any], result: dict[str, Any]) -> None:
    if "error" in result:
        catalog.add_issue(None, row["rel_path"], "hash_error", result["error"])
        return
    catalog.set_analysis(row["id"], row["size"], row["mtime_ns"], **result)
