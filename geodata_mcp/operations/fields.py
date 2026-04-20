"""In-place field ops: add_field, update_field, drop_field, annotate."""
from __future__ import annotations

from datetime import datetime

from ..loader import (
    LoadError as OpError,
    _column_schema,
    _quote_ident,
)
from ..session import Operation, Session
from ._util import _require_layer, decode_escapes_deep
from .checkpoint import _reversible_for_layer, _snapshot_column
from .sql import _validate_sql


_FIELD_TYPES = {
    "int": "BIGINT", "bigint": "BIGINT", "integer": "BIGINT",
    "float": "DOUBLE", "double": "DOUBLE", "real": "DOUBLE",
    "bool": "BOOLEAN", "boolean": "BOOLEAN",
    "str": "VARCHAR", "string": "VARCHAR", "varchar": "VARCHAR", "text": "VARCHAR",
    "date": "DATE", "timestamp": "TIMESTAMP",
}


def _normalize_field_type(t: str) -> str:
    lt = (t or "").strip().lower()
    return _FIELD_TYPES.get(lt, t.upper() if t else "VARCHAR")


def _sanity_check_expr(expr: str) -> None:
    """Validate an expression destined for CTAS / UPDATE. `_validate_sql`
    parses the embedded SELECT, rejects multi-statement input, and enforces
    the execute_sql denylist (file readers / URLs / abs-paths / big
    numeric literals)."""
    _validate_sql(f"SELECT ({expr}) AS _x")


def add_field(
    session: Session, layer: str, name: str, expr: str,
    field_type: str | None = None,
) -> dict:
    """Add a new column to `layer` whose values come from a SQL expression
    evaluated per row. In-place (mutates the layer). If a checkpoint is active,
    the change is reversible via rollback().

    Args:
        layer: target layer name.
        name: new column name. Must not already exist.
        expr: a SQL scalar expression valid in DuckDB's SELECT list. May
              reference other columns of the same layer. May also reference
              other session layers via scalar subqueries.
        field_type: optional DuckDB type (VARCHAR, DOUBLE, BIGINT, BOOLEAN, ...).
                    If omitted, the expression's type is inferred.
    """
    meta = _require_layer(session, layer)
    if name.startswith("__"):
        raise OpError(f"column name '{name}' reserved (starts with '__')")
    if name in meta.attributes and not name.startswith("__"):
        raise OpError(
            f"column '{name}' already exists on '{layer}'. "
            f"Use update_field() to overwrite or pick another name."
        )
    _sanity_check_expr(expr)

    # Snapshot BEFORE mutating if a checkpoint is active.
    _snapshot_column(session, layer, name)

    qlayer = _quote_ident(layer)
    qcol = _quote_ident(name)

    if field_type:
        dtype = _normalize_field_type(field_type)
        try:
            session.conn.execute(f"ALTER TABLE {qlayer} ADD COLUMN {qcol} {dtype}")
            session.conn.execute(f"UPDATE {qlayer} SET {qcol} = ({expr})")
        except Exception as e:
            raise OpError(f"add_field failed: {type(e).__name__}: {e}")
    else:
        # Use DuckDB's ALTER TABLE ADD COLUMN with a default expression, which
        # auto-infers the type. Doesn't work for all DuckDB versions; fall
        # back to CTAS-then-swap if needed.
        try:
            session.conn.execute(
                f"ALTER TABLE {qlayer} ADD COLUMN {qcol} AS ({expr})"
            )
        except Exception:
            # Fallback: infer type by running the expr once, then add & update.
            try:
                probe = session.conn.execute(
                    f"SELECT typeof(({expr})) FROM {qlayer} LIMIT 1"
                ).fetchone()
                inferred = (probe[0] if probe else "VARCHAR")
                session.conn.execute(f"ALTER TABLE {qlayer} ADD COLUMN {qcol} {inferred}")
                session.conn.execute(f"UPDATE {qlayer} SET {qcol} = ({expr})")
            except Exception as e:
                raise OpError(f"add_field failed: {type(e).__name__}: {e}")

    # Refresh meta (schema + feature count unchanged).
    schema = _column_schema(session.conn, layer)
    # Preserve geometry-col marker.
    geom_marker = meta.attributes.get("__geom_col__", "")
    meta.attributes = schema
    meta.attributes["__geom_col__"] = geom_marker
    meta.column_provenance[name] = {
        "authored_by": "derived",
        "tool": "add_field",
        "at": datetime.utcnow().isoformat() + "Z",
        "expr": expr,
    }
    session.log(Operation(
        tool="add_field",
        args={"layer": layer, "name": name, "expr": expr, "field_type": field_type},
        result_layer=layer, summary=f"added {name} to {layer}",
        at=datetime.utcnow(),
    ))
    covering = _reversible_for_layer(session, layer)
    return {
        "layer": layer,
        "column": name,
        "columns_now": [c for c in meta.attributes if not c.startswith("__")],
        "reversible": bool(covering),
        "covering_checkpoints": covering,
    }


def update_field(
    session: Session, layer: str, name: str, expr: str,
    where: str | None = None,
) -> dict:
    """Overwrite an existing column with values from a SQL expression.
    Optional WHERE restricts the rows updated.
    """
    # Lazy import to keep the dependency DAG readable — query.py does not
    # import fields.py, but _assert_predicate lives in _util.
    from ._util import _assert_predicate

    meta = _require_layer(session, layer)
    if name not in meta.attributes or name.startswith("__"):
        raise OpError(f"column '{name}' not on '{layer}'. "
                      f"Available: {[c for c in meta.attributes if not c.startswith('__')]}")
    _sanity_check_expr(expr)
    if where:
        _assert_predicate(where, "where")
        _validate_sql(f"SELECT 1 FROM _t WHERE ({where})")

    _snapshot_column(session, layer, name)

    qlayer = _quote_ident(layer)
    qcol = _quote_ident(name)
    where_sql = f" WHERE {where}" if where else ""
    try:
        n_before = session.conn.execute(f"SELECT COUNT(*) FROM {qlayer}{where_sql}").fetchone()[0]
        session.conn.execute(f"UPDATE {qlayer} SET {qcol} = ({expr}){where_sql}")
    except Exception as e:
        raise OpError(f"update_field failed: {type(e).__name__}: {e}")

    meta.column_provenance[name] = {
        "authored_by": "derived",
        "tool": "update_field",
        "at": datetime.utcnow().isoformat() + "Z",
        "expr": expr,
        "where": where,
    }
    session.log(Operation(
        tool="update_field",
        args={"layer": layer, "name": name, "expr": expr, "where": where},
        result_layer=layer, summary=f"updated {name} on {int(n_before)} rows",
        at=datetime.utcnow(),
    ))
    covering = _reversible_for_layer(session, layer)
    return {
        "layer": layer, "column": name, "rows_updated": int(n_before),
        "reversible": bool(covering),
        "covering_checkpoints": covering,
    }


def drop_field(session: Session, layer: str, name: str) -> dict:
    """Drop a column from a layer. In-place."""
    meta = _require_layer(session, layer)
    if name not in meta.attributes or name.startswith("__"):
        raise OpError(f"column '{name}' not on '{layer}'.")
    geom_col = meta.attributes.get("__geom_col__") or ""
    if name == geom_col:
        raise OpError(f"cannot drop geometry column '{name}' (use drop_layer instead)")

    _snapshot_column(session, layer, name)
    qlayer = _quote_ident(layer)
    qcol = _quote_ident(name)
    try:
        session.conn.execute(f"ALTER TABLE {qlayer} DROP COLUMN {qcol}")
    except Exception as e:
        raise OpError(f"drop_field failed: {type(e).__name__}: {e}")
    meta.attributes.pop(name, None)
    meta.column_provenance.pop(name, None)
    session.log(Operation(
        tool="drop_field", args={"layer": layer, "name": name},
        result_layer=layer, summary=f"dropped {name}",
        at=datetime.utcnow(),
    ))
    return {"layer": layer, "dropped": name,
            "columns_now": [c for c in meta.attributes if not c.startswith("__")]}


# ---------- annotate (LLM-classified per-feature attributes) ----------

ANNOTATE_CAP = 10_000


def annotate(
    session: Session, layer: str,
    values: dict,
    key_column: str = "rowid",
    dry_run: bool = False,
    model: str | None = None,
) -> dict:
    """Apply per-feature attribute values by key. Each `values` entry is
    `{key_value: {attr_name: attr_value, ...}}`. Missing attributes are created
    as new columns (VARCHAR by default). Keys not in the layer are reported.

    This is the standard "LLM classifies features" pattern: `inspect` or
    `batch_iterate` to see the feature IDs + attributes, reason, then one
    call of annotate with the whole {id: {...}} map.

    Args:
        layer: target layer.
        values: dict of {key_value: {col: val, ...}}. Up to ANNOTATE_CAP entries.
        key_column: column to match on (default 'rowid' — DuckDB's implicit
                    rowid pseudo-column). Use a declared column (e.g. 'id')
                    when you have one; rowid is fine for ephemeral flows.
        dry_run: if True, validate key matching and preview coverage without
                 writing. Returns the same response shape with `dry_run: True`
                 and no mutation; nothing is committed, no columns are created.
        model: optional identifier for the LLM/tool that authored these
               values. Stored in per-column provenance so an exported column
               can be traced back to its author.
    """
    meta = _require_layer(session, layer)
    if not values:
        raise OpError("`values` must not be empty")
    if len(values) > ANNOTATE_CAP:
        raise OpError(f"too many annotations ({len(values)}); cap is {ANNOTATE_CAP}")

    # Normalise literal \uXXXX escapes in user-supplied values — some MCP
    # clients over-escape unicode when building tool-call JSON, and the
    # annotated values end up displayed in the viewer's popup.
    values = decode_escapes_deep(values)

    # Collect all attribute names across values.
    all_attrs: dict[str, set] = {}
    for k, row in values.items():
        if not isinstance(row, dict):
            raise OpError(f"values['{k}'] must be a dict, got {type(row).__name__}")
        for col, v in row.items():
            if col.startswith("__"):
                raise OpError(f"attribute '{col}' reserved (starts with '__')")
            all_attrs.setdefault(col, set()).add(type(v).__name__)

    # Validate key column exists (rowid is fine — it's a pseudo-column in DuckDB).
    declared_cols = [c for c in meta.attributes if not c.startswith("__")]
    if key_column != "rowid" and key_column not in declared_cols:
        raise OpError(
            f"key_column '{key_column}' not on layer '{layer}'. "
            f"Available: {declared_cols + ['rowid']}"
        )

    # Dry-run: report match coverage without writing anything.
    if dry_run:
        qlayer_dr = _quote_ident(layer)
        qkey_dr = _quote_ident(key_column)
        try:
            import pyarrow as pa
        except ImportError:
            raise OpError("pyarrow is required for annotate")
        keys_tbl = pa.Table.from_pylist([{"__key": k} for k in values.keys()])
        tmp_dr = "_annotate_dry_tmp"
        session.conn.register(tmp_dr, keys_tbl)
        try:
            matched_dr = session.conn.execute(
                f"SELECT COUNT(*) FROM {tmp_dr} t WHERE EXISTS "
                f"(SELECT 1 FROM {qlayer_dr} WHERE {qkey_dr} = t.__key)"
            ).fetchone()
            keys_matched = int(matched_dr[0]) if matched_dr else 0
        finally:
            session.conn.unregister(tmp_dr)
        attr_order_dr = list(all_attrs.keys())
        new_cols = [c for c in attr_order_dr if c not in meta.attributes]
        return {
            "layer": layer,
            "dry_run": True,
            "attributes_would_write": attr_order_dr,
            "new_columns_would_create": new_cols,
            "keys_submitted": len(values),
            "keys_matched": keys_matched,
            "keys_unmatched": len(values) - keys_matched,
            "rows_total": meta.feature_count,
            "coverage_pct_if_applied": round(
                100.0 * keys_matched / meta.feature_count, 2
            ) if meta.feature_count else 0.0,
        }

    # Snapshot columns that will be written (new or existing).
    for col in all_attrs:
        _snapshot_column(session, layer, col)

    qlayer = _quote_ident(layer)

    # Ensure every target column exists. Create missing ones as VARCHAR
    # (the LLM's natural output form; floats/ints round-trip fine).
    for col, kinds in all_attrs.items():
        if col in meta.attributes and not col.startswith("__"):
            continue
        # Pick a type: if all values are numeric → DOUBLE; bool → BOOLEAN; else VARCHAR.
        kinds = {k for k in kinds if k != "NoneType"}
        if kinds <= {"int"}:
            dtype = "BIGINT"
        elif kinds <= {"int", "float"}:
            dtype = "DOUBLE"
        elif kinds <= {"bool"}:
            dtype = "BOOLEAN"
        else:
            dtype = "VARCHAR"
        try:
            session.conn.execute(f"ALTER TABLE {qlayer} ADD COLUMN {_quote_ident(col)} {dtype}")
        except Exception as e:
            raise OpError(f"annotate failed adding column '{col}': {type(e).__name__}: {e}")

    # Build the update table and apply per-row UPDATEs in a single batch.
    try:
        import pyarrow as pa
    except ImportError:
        raise OpError("pyarrow is required for annotate")

    rows = []
    attr_order = list(all_attrs.keys())
    for k, row in values.items():
        rec = {"__key": k}
        for col in attr_order:
            rec[col] = row.get(col)
        rows.append(rec)
    table = pa.Table.from_pylist(rows)

    tmp = "_annotate_tmp"
    session.conn.register(tmp, table)
    try:
        # One UPDATE per attribute column (DuckDB-friendly) scoped by key match.
        unmatched = 0
        for col in attr_order:
            qcol = _quote_ident(col)
            sql = (
                f"UPDATE {qlayer} SET {qcol} = t.{qcol} "
                f"FROM {tmp} t WHERE {qlayer}.{_quote_ident(key_column)} = t.__key"
            )
            session.conn.execute(sql)
        # Count unmatched keys.
        unmatched_rows = session.conn.execute(
            f"SELECT COUNT(*) FROM {tmp} t WHERE NOT EXISTS "
            f"(SELECT 1 FROM {qlayer} WHERE {_quote_ident(key_column)} = t.__key)"
        ).fetchone()
        unmatched = int(unmatched_rows[0]) if unmatched_rows else 0
    except Exception as e:
        raise OpError(f"annotate failed: {type(e).__name__}: {e}")
    finally:
        session.conn.unregister(tmp)

    schema = _column_schema(session.conn, layer)
    geom_marker = meta.attributes.get("__geom_col__", "")
    meta.attributes = schema
    meta.attributes["__geom_col__"] = geom_marker

    # Row-level coverage: how many rows in the target layer now have a
    # non-NULL value in *any* of the columns we just wrote. Closes the
    # silent-coverage-gap when the caller forgets to annotate some rows
    # even if every key they submitted matched.
    try:
        any_non_null = " OR ".join(
            f"{_quote_ident(c)} IS NOT NULL" for c in attr_order
        )
        cov = session.conn.execute(
            f"SELECT COUNT(*) FROM {qlayer} WHERE {any_non_null}"
        ).fetchone()
        rows_with_any = int(cov[0]) if cov else 0
    except Exception:
        rows_with_any = 0
    rows_without_any = max(0, meta.feature_count - rows_with_any)

    # Per-column provenance stamp. Overwrites on re-annotate — the latest
    # authoring wins. Existing human-authored columns that get re-annotated
    # now reflect that the overwrite happened.
    now_iso = datetime.utcnow().isoformat() + "Z"
    for col in attr_order:
        meta.column_provenance[col] = {
            "authored_by": "llm",
            "tool": "annotate",
            "at": now_iso,
            "model": model,
        }

    session.log(Operation(
        tool="annotate",
        args={"layer": layer, "n_keys": len(values),
              "attributes": attr_order, "key_column": key_column},
        result_layer=layer, summary=f"annotated {len(values)-unmatched}/{len(values)} keys",
        at=datetime.utcnow(),
    ))
    covering = _reversible_for_layer(session, layer)
    out = {
        "layer": layer,
        "attributes_written": attr_order,
        "keys_submitted": len(values),
        "keys_matched": len(values) - unmatched,
        "keys_unmatched": unmatched,
        "keys_cap": ANNOTATE_CAP,
        "rows_total": meta.feature_count,
        "rows_with_any_annotation": rows_with_any,
        "rows_without_annotation": rows_without_any,
        "coverage_pct": round(
            100.0 * rows_with_any / meta.feature_count, 2
        ) if meta.feature_count else 0.0,
        "reversible": bool(covering),
        "covering_checkpoints": covering,
    }
    if rows_without_any:
        out["coverage_hint"] = (
            f"{rows_without_any} of {meta.feature_count} rows have no value "
            f"in any of the new columns. If that is unintended, check that "
            f"your `values` dict covers every target feature."
        )
    if unmatched:
        out["warning"] = (
            f"{unmatched} of {len(values)} keys did not match any row in "
            f"'{layer}' on column '{key_column}' — they were skipped. Check "
            "key_column spelling and value types."
        )
    if len(values) >= ANNOTATE_CAP:
        out["hint"] = (
            f"Used {len(values)} / {ANNOTATE_CAP} cap. For larger payloads, use "
            "create_layer(rows=...) as a side-table + add_field() with a subquery "
            "join, which has no inline-JSON size pressure."
        )
    return out
