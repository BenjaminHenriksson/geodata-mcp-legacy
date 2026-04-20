"""Checkpoint / rollback / commit — snapshot-based undo for in-place
mutations on session layers."""
from __future__ import annotations

from datetime import datetime

from ..loader import LoadError as OpError, _quote_ident
from ..session import Operation, Session


def _active_checkpoints_for(session: Session, layer: str) -> list[dict]:
    """Return every active checkpoint whose scope covers `layer`. A checkpoint
    with `layers=None` covers every layer; a scoped one covers only its listed
    layers. Empty list means this mutation is not being tracked by any
    checkpoint."""
    out = []
    for ckpt in session.checkpoints.values():
        scope = ckpt.get("layers")
        if scope is None or layer in scope:
            out.append(ckpt)
    return out


def _reversible_for_layer(session: Session, layer: str) -> list[str]:
    """List of checkpoint names that would roll back a mutation on `layer`."""
    return [name for name, ckpt in session.checkpoints.items()
            if ckpt.get("layers") is None or layer in ckpt["layers"]]


def _snapshot_column(session: Session, layer: str, column: str) -> None:
    """For every active checkpoint that covers `layer`, snapshot the (layer,
    column) pre-image (idempotent per-checkpoint). Called before in-place
    column mutations."""
    covering = _active_checkpoints_for(session, layer)
    if not covering:
        return
    meta = session.layers.get(layer)
    col_exists = bool(meta and column in meta.attributes and not column.startswith("__"))
    for ckpt in covering:
        # idempotent per checkpoint
        if any(s["layer"] == layer and s["column"] == column
               for s in ckpt["snapshots"]):
            continue
        cid = ckpt["id"]
        snap_table = f"_snap_{cid}_{layer}_{column}"
        if col_exists:
            session.conn.execute(
                f"CREATE TABLE {_quote_ident(snap_table)} AS "
                f"SELECT rowid AS __rowid, {_quote_ident(column)} "
                f"FROM {_quote_ident(layer)}"
            )
        ckpt["snapshots"].append({
            "layer": layer, "column": column, "snap_table": snap_table,
            "column_existed": col_exists, "whole_layer": False,
        })


def _snapshot_whole_layer(session: Session, layer: str) -> None:
    """Snapshot an entire layer across every covering active checkpoint."""
    covering = _active_checkpoints_for(session, layer)
    if not covering:
        return
    for ckpt in covering:
        if any(s["layer"] == layer and s.get("whole_layer")
               for s in ckpt["snapshots"]):
            continue
        cid = ckpt["id"]
        snap_table = f"_snap_{cid}_{layer}__full"
        session.conn.execute(
            f"CREATE TABLE {_quote_ident(snap_table)} AS "
            f"SELECT * FROM {_quote_ident(layer)}"
        )
        ckpt["snapshots"].append({
            "layer": layer, "column": None, "snap_table": snap_table,
            "column_existed": True, "whole_layer": True,
            "meta_snapshot": session.layers.get(layer),
        })


def checkpoint(
    session: Session, name: str,
    layers: list[str] | None = None,
) -> dict:
    """Create a named checkpoint. With `layers=None` the checkpoint covers
    every layer in the session; passing a list scopes it to those layers.
    Multiple checkpoints can be active at once — mutations are snapshotted
    for every covering checkpoint."""
    if name in session.checkpoints:
        raise OpError(f"checkpoint '{name}' already exists. Commit or rollback first.")
    if layers is not None:
        # Validate the scope layers exist now (not required later; mutations
        # on layers not yet created at checkpoint time are simply not covered).
        missing = [l for l in layers if l not in session.layers]
        if missing:
            raise OpError(
                f"checkpoint scope references unknown layers: {missing}. "
                f"Available: {list(session.layers)}"
            )
    session._next_checkpoint_id += 1
    session.checkpoints[name] = {
        "id": session._next_checkpoint_id,
        "snapshots": [],
        "created_at": datetime.utcnow().isoformat() + "Z",
        "layers": set(layers) if layers else None,
    }
    session.active_checkpoint = name  # "most recent" for hint compat
    session.log(Operation(
        tool="checkpoint", args={"name": name, "layers": layers},
        result_layer=None,
        summary=(f"checkpoint set (scope: {layers})" if layers else "checkpoint set (all layers)"),
        at=datetime.utcnow(),
    ))
    return {
        "checkpoint": name,
        "status": "active",
        "scope": "all_layers" if layers is None else list(layers),
        "other_active": [n for n in session.checkpoints if n != name],
    }


def rollback(session: Session, name: str) -> dict:
    """Restore all mutations since `checkpoint(name)`. Snapshots are dropped."""
    ckpt = session.checkpoints.get(name)
    if ckpt is None:
        raise OpError(f"unknown checkpoint '{name}'. Active: {list(session.checkpoints)}")
    # Replay in reverse order (latest snapshot first).
    for s in reversed(ckpt["snapshots"]):
        snap_table = s["snap_table"]
        if s.get("whole_layer"):
            # Layer was dropped or renamed-away under this checkpoint.
            # Drop any current version, then re-create from snapshot + restore meta.
            qlayer = _quote_ident(s["layer"])
            try:
                session.conn.execute(f"DROP TABLE IF EXISTS {qlayer}")
            except Exception:
                pass
            session.conn.execute(
                f"CREATE TABLE {qlayer} AS SELECT * FROM {_quote_ident(snap_table)}"
            )
            if s.get("meta_snapshot"):
                session.layers[s["layer"]] = s["meta_snapshot"]
        else:
            qlayer = _quote_ident(s["layer"])
            qcol = _quote_ident(s["column"])
            qsnap = _quote_ident(snap_table)
            if s["column_existed"]:
                # Restore the pre-image values. Column may currently be of a
                # different type; DuckDB's UPDATE will coerce if compatible.
                session.conn.execute(
                    f"UPDATE {qlayer} SET {qcol} = (SELECT {qcol} FROM {qsnap} "
                    f"WHERE {qsnap}.__rowid = {qlayer}.rowid)"
                )
            else:
                # Column did not exist before the checkpoint — drop it.
                try:
                    session.conn.execute(f"ALTER TABLE {qlayer} DROP COLUMN {qcol}")
                except Exception:
                    pass
                meta = session.layers.get(s["layer"])
                if meta and s["column"] in meta.attributes:
                    meta.attributes.pop(s["column"])
        try:
            session.conn.execute(f"DROP TABLE IF EXISTS {_quote_ident(snap_table)}")
        except Exception:
            pass
    snap_count = len(ckpt["snapshots"])
    del session.checkpoints[name]
    if session.active_checkpoint == name:
        session.active_checkpoint = next(iter(session.checkpoints), None)
    session.log(Operation(
        tool="rollback", args={"name": name},
        result_layer=None, summary=f"restored {snap_count} snapshots",
        at=datetime.utcnow(),
    ))
    return {"checkpoint": name, "status": "rolled_back", "restored": snap_count,
            "other_active": list(session.checkpoints)}


def commit(session: Session, name: str) -> dict:
    """Discard the snapshots for a checkpoint (making the mutations permanent)."""
    ckpt = session.checkpoints.get(name)
    if ckpt is None:
        raise OpError(f"unknown checkpoint '{name}'. Active: {list(session.checkpoints)}")
    for s in ckpt["snapshots"]:
        try:
            session.conn.execute(f"DROP TABLE IF EXISTS {_quote_ident(s['snap_table'])}")
        except Exception:
            pass
    snap_count = len(ckpt["snapshots"])
    del session.checkpoints[name]
    if session.active_checkpoint == name:
        session.active_checkpoint = next(iter(session.checkpoints), None)
    session.log(Operation(
        tool="commit", args={"name": name},
        result_layer=None, summary=f"committed, {snap_count} snapshots discarded",
        at=datetime.utcnow(),
    ))
    return {"checkpoint": name, "status": "committed",
            "discarded_snapshots": snap_count,
            "other_active": list(session.checkpoints)}
