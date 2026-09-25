"""Manual-mode actions on items: keep, to delete, restore, undo last (spec §3.3 table)."""

from __future__ import annotations

from framesift.engine.catalog import Catalog
from framesift.engine.config import Roots
from framesift.engine.journal import (
    MoveOutcome,
    item_move_specs,
    perform_moves,
    record_keep,
    undo_ops,
)


class ActionError(Exception):
    pass


def _primary(catalog: Catalog, item_id: int):
    row = catalog.file(item_id)
    if row is None or row["status"] != "present":
        raise ActionError(f"item {item_id} is not present")
    return row


def keep(catalog: Catalog, roots: Roots, item_id: int, session_id: int) -> str | MoveOutcome:
    """→ Source: mark reviewed. Review/Delete: return to the original place (+ reviewed for Review)."""
    row = _primary(catalog, item_id)
    if row["root"] == "source":
        return record_keep(catalog, session_id, item_id)
    outcome = restore(catalog, roots, item_id, session_id)
    if row["root"] == "review":
        for f in catalog.item_files(item_id):
            catalog.set_reviewed(f["id"], True)
        catalog.commit()
    return outcome


def to_delete(catalog: Catalog, roots: Roots, item_id: int, session_id: int) -> MoveOutcome | None:
    """← Source/Review: move to <Delete>/<source-relative path>. Delete: nothing."""
    row = _primary(catalog, item_id)
    if row["root"] == "delete":
        return None
    specs = item_move_specs(catalog, item_id, "delete")
    return perform_moves(catalog, roots, specs, session_id=session_id, action="to_delete")


def restore(catalog: Catalog, roots: Roots, item_id: int, session_id: int) -> MoveOutcome:
    """Review/Delete → original place in Source."""
    row = _primary(catalog, item_id)
    if row["root"] == "source":
        raise ActionError("item is already in the source folder")
    if not row["origin_rel_path"]:
        raise ActionError("item has no recorded original location")
    specs = item_move_specs(catalog, item_id, "source")
    return perform_moves(catalog, roots, specs, session_id=session_id, action="restore")


def undo_last(catalog: Catalog, roots: Roots, session_id: int) -> str | None:
    ops = catalog.undoable_ops(session_id=session_id, limit=1)
    if not ops:
        return None
    undo_ops(catalog, roots, ops, session_id=session_id)
    return ops[0]
