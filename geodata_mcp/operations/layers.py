"""Layer-level ops: create_layer, drop_layer, rename_layer, list_layers,
set_notes, hide_layers."""
from __future__ import annotations

from datetime import datetime

from ..loader import (
    LoadError as OpError,
    _bbox_from_table,
    _column_schema,
    _detect_geometry_column,
    _quote_ident,
)
from ..session import LayerMeta, Operation, Session, SourceRef
from ._util import _EPSG_RE, _probe_geom_type, _require_layer, decode_unicode_escapes
from .checkpoint import _reversible_for_layer, _snapshot_whole_layer


# ---------- create_layer (Phase 3) ----------


CREATE_LAYER_CAP = 1_000


def create_layer(
    session: Session, name: str,
    data: list[dict],
    *,
    source: str | None = None,
    geometry_column: str | None = None,
    crs: str = "EPSG:4326",
) -> LayerMeta:
    """Inject LLM-provided data as a new session layer.

    A `source` attribute is always present on every row of the resulting
    layer so provenance survives filtering, joining, and export. Per-row
    `source` values in `data` win; otherwise the top-level `source` arg is
    broadcast to every row. One or the other MUST be supplied.

    Args:
        name: Desired layer name (may be suffixed if it collides).
        data: List of dicts. Each dict = one row. All rows should share keys.
              If a row has a `source` field, it is preserved (per-row
              provenance — e.g. mixing hitta.se + booli.se entries); if it
              doesn't, the row gets the top-level `source` value.
        source: Free-text description of where the data came from (required
                unless every row already carries its own `source`).
        geometry_column: If set, this column in each row contains WKT geometry
                         that will be parsed and reprojected to EPSG:3011.
        crs: EPSG code of the input geometry. Default 'EPSG:4326' (lng/lat).

    Returns the new layer's LayerMeta.
    """
    if not data:
        raise OpError("`data` must not be empty")
    if len(data) > CREATE_LAYER_CAP:
        raise OpError(f"`data` has {len(data)} rows; cap is {CREATE_LAYER_CAP}")

    try:
        import pyarrow as pa
    except ImportError:
        raise OpError("pyarrow is required for create_layer but not installed")

    # Provenance guarantee: every row gets a `source` string.
    # Acceptable inputs:
    #   (1) top-level `source="..."` and no `source` in any row → broadcast
    #   (2) per-row `source` on every row → use as-is
    #   (3) mix — per-row wins, top-level fills the gaps
    #   (4) neither → OpError (cannot inject data with no provenance)
    any_row_has_source = any("source" in r for r in data)
    if not source and not any_row_has_source:
        raise OpError(
            "`source` is required — either pass a top-level source='<where this "
            "came from>' string, or include a 'source' field in every row. "
            "Every injected row must carry provenance so downstream filters/joins "
            "preserve the origin."
        )
    # Broadcast / fill. Mutate the caller's dict entries in place — simpler
    # than reconstructing the list.
    filled = []
    for r in data:
        row = dict(r)
        if row.get("source") in (None, ""):
            row["source"] = source or ""
        filled.append(row)
    data = filled

    # Build an Arrow table. from_pylist auto-infers types column by column.
    try:
        table = pa.Table.from_pylist(data)
    except Exception as e:
        raise OpError(f"could not coerce data to a table: {type(e).__name__}: {e}")

    columns = [str(f.name) for f in table.schema]
    if geometry_column and geometry_column not in columns:
        raise OpError(f"geometry_column '{geometry_column}' not present in data keys")

    new_name = session.unique_layer_name(name)
    qnew = _quote_ident(new_name)

    # Strict whitelist: CRS strings MUST match EPSG:<digits>. This runs before
    # the value is interpolated into a literal that reaches DuckDB directly
    # (not through _validate_sql). Without the whitelist, a crafted `crs`
    # could close the ST_Transform arg and append arbitrary SQL — including
    # DuckDB's filesystem / httpfs / extension-loader functions that the
    # execute_sql sandbox denylists.
    if geometry_column:
        if not _EPSG_RE.fullmatch(crs or ""):
            raise OpError(
                f"crs must match 'EPSG:<digits>' (got {crs!r}). Examples: "
                "'EPSG:4326' (WGS84 lng/lat), 'EPSG:3011' (SWEREF99 18 00)."
            )

    # Register the arrow table so we can CREATE TABLE AS SELECT from it.
    tmp = "_create_layer_tmp"
    session.conn.register(tmp, table)
    try:
        if geometry_column:
            other_cols = [c for c in columns if c != geometry_column]
            attr_select = ", ".join(_quote_ident(c) for c in other_cols)
            sql = (
                f"CREATE TABLE {qnew} AS "
                f"SELECT {attr_select + ',' if attr_select else ''} "
                f"ST_Transform(ST_GeomFromText({_quote_ident(geometry_column)}), "
                f"'{crs}', 'EPSG:3011', true) AS geom "
                f"FROM {tmp}"
            )
        else:
            sql = f"CREATE TABLE {qnew} AS SELECT * FROM {tmp}"
        try:
            session.conn.execute(sql)
        except Exception as e:
            raise OpError(f"could not materialize layer: {type(e).__name__}: {e}")
    finally:
        session.conn.unregister(tmp)

    # Build a provenance ref marked as LLM-sourced.
    prov = SourceRef(
        dataset_id=None,
        source_name=source or f"LLM-provided layer '{new_name}'",
        publisher="(LLM-provided)",
        license="(LLM-provided)",
        url=None, file_path=None, retrieved=datetime.utcnow().date().isoformat(),
        llm_sourced=True,
        llm_source_description=source,
    )

    schema = _column_schema(session.conn, new_name)
    geom_col = _detect_geometry_column(schema)
    bbox = _bbox_from_table(session.conn, new_name, geom_col) if geom_col else None
    n = session.conn.execute(f"SELECT COUNT(*) FROM {qnew}").fetchone()[0]
    geom_type = _probe_geom_type(session, new_name, geom_col) if geom_col else None

    meta = LayerMeta(
        name=new_name, feature_count=int(n),
        geometry_type=geom_type,
        bbox=bbox, attributes=schema,
        created_by="create_layer", created_at=datetime.utcnow(),
        provenance=[prov], parent_layers=[],
    )
    meta.attributes["__geom_col__"] = geom_col or ""
    session.register(meta)
    session.log(Operation(
        tool="create_layer",
        args={"name": new_name, "rows": len(data), "source": source,
              "geometry_column": geometry_column, "crs": crs if geometry_column else None},
        result_layer=new_name, summary=f"{n} rows (LLM-sourced)",
        at=datetime.utcnow(),
    ))
    return meta


# ---------- drop_layer / rename_layer ----------

def drop_layer(session: Session, name: str) -> dict:
    _require_layer(session, name)
    # If a checkpoint is active, snapshot the whole layer so rollback can restore.
    _snapshot_whole_layer(session, name)
    try:
        session.conn.execute(f"DROP TABLE IF EXISTS {_quote_ident(name)}")
    except Exception as e:
        raise OpError(f"drop_layer failed: {type(e).__name__}: {e}")
    session.layers.pop(name, None)
    if name in session.visible_layers:
        session.visible_layers.remove(name)
    session.log(Operation(
        tool="drop_layer", args={"name": name},
        result_layer=None, summary=f"dropped {name}",
        at=datetime.utcnow(),
    ))
    return {"dropped": name, "remaining_layers": list(session.layers)}


def rename_layer(session: Session, old: str, new: str) -> dict:
    _require_layer(session, old)
    if new in session.layers:
        raise OpError(f"target name '{new}' already exists")
    if old == new:
        raise OpError("old and new names are identical")
    # Snapshot both sides: the old layer (so rollback can recreate under old name)
    # and the new layer (as a fresh table — rollback will drop it).
    _snapshot_whole_layer(session, old)

    try:
        session.conn.execute(
            f"ALTER TABLE {_quote_ident(old)} RENAME TO {_quote_ident(new)}"
        )
    except Exception as e:
        raise OpError(f"rename_layer failed: {type(e).__name__}: {e}")
    meta = session.layers.pop(old)
    meta.name = new
    session.layers[new] = meta
    session.visible_layers = [new if n == old else n for n in session.visible_layers]
    session.log(Operation(
        tool="rename_layer", args={"old": old, "new": new},
        result_layer=new, summary=f"{old} → {new}",
        at=datetime.utcnow(),
    ))
    return {"renamed": {"from": old, "to": new}}


# ---------- list_layers + set_notes + hide_layers ----------

def list_layers(session: Session) -> dict:
    """Inventory of all session layers."""
    items = []
    for name, m in session.layers.items():
        items.append({
            "name": name,
            "feature_count": m.feature_count,
            "geometry_type": m.geometry_type,
            "bbox_3011": list(m.bbox) if m.bbox else None,
            "columns": [c for c in m.attributes if not c.startswith("__")],
            "created_by": m.created_by,
            "parent_layers": m.parent_layers,
            "notes": m.notes,
            "is_visible": name in session.visible_layers,
            "covering_checkpoints": _reversible_for_layer(session, name),
        })
    ckpts = []
    for cname, ckpt in session.checkpoints.items():
        scope = ckpt.get("layers")
        ckpts.append({
            "name": cname,
            "scope": "all_layers" if scope is None else sorted(scope),
            "n_snapshots": len(ckpt["snapshots"]),
        })
    return {
        "n_layers": len(items),
        "layers": items,
        "checkpoints": ckpts,
        "most_recent_checkpoint": session.active_checkpoint,
    }


def set_notes(session: Session, layer: str, notes: str) -> dict:
    """Attach a free-text note to a layer. Shown in list_layers and sources."""
    meta = _require_layer(session, layer)
    notes = decode_unicode_escapes(notes)
    meta.notes = notes
    session.log(Operation(
        tool="set_notes", args={"layer": layer, "notes_len": len(notes)},
        result_layer=layer, summary=f"notes updated ({len(notes)} chars)",
        at=datetime.utcnow(),
    ))
    return {"layer": layer, "notes_chars": len(notes)}


def hide_layers(session: Session, layers: list[str] | None = None) -> dict:
    """Remove layers from the viewer's visible list. With `layers=None` hides
    all."""
    if not layers:
        hidden = list(session.visible_layers)
        session.visible_layers = []
    else:
        hidden = [n for n in layers if n in session.visible_layers]
        session.visible_layers = [n for n in session.visible_layers if n not in layers]
    session.bump_version()
    session.log(Operation(
        tool="hide", args={"layers": layers},
        result_layer=None, summary=f"hid {len(hidden)} layers",
        at=datetime.utcnow(),
    ))
    return {
        "hidden": hidden,
        "still_visible": list(session.visible_layers),
    }
