"""Read / derive operations: filter_layer, stats, frequencies, baseline_stats,
top_n, classify, batch_iterate."""
from __future__ import annotations

import re
from datetime import datetime

from ..loader import LoadError as OpError, _quote_ident
from ..session import LayerMeta, Operation, Session
from ._util import (
    _assert_predicate,
    _assert_single_sql_expression,
    _fmt_cell,
    _no_semicolon,
    _register_result,
    _require_layer,
    _sql_literal,
    is_numeric_sql_type,
)


_TRAILING_ORDER_RE = re.compile(
    r"\s+(ASC|DESC)(\s+NULLS\s+(FIRST|LAST))?\s*$",
    re.IGNORECASE,
)


def _split_order_direction(by: str) -> tuple[str, str | None]:
    """Split `'col DESC NULLS LAST'` into `('col', 'DESC NULLS LAST')`.

    If `by` ends with an ordering keyword (`ASC|DESC`, optionally with
    `NULLS FIRST|LAST`), strip it and return it separately so the
    caller can honour it instead of double-appending its own. Returns
    `(expr, None)` when no trailing keyword is found.
    """
    m = _TRAILING_ORDER_RE.search(by)
    if not m:
        return by, None
    expr = by[: m.start()].rstrip()
    direction = " ".join(m.group(0).split()).upper()
    return expr, direction


# ---------- filter ----------


def filter_layer(
    session: Session, source_layer: str, where: str,
    *, result_name: str | None = None,
) -> LayerMeta:
    src = _require_layer(session, source_layer)
    _no_semicolon(where, "where")
    new_name = session.unique_layer_name(result_name or f"{source_layer}_filtered")
    session.conn.execute(
        f"CREATE TABLE {_quote_ident(new_name)} AS "
        f"SELECT * FROM {_quote_ident(source_layer)} WHERE {where}"
    )
    meta = _register_result(session, new_name, [source_layer], created_by="filter")
    session.log(Operation(
        tool="filter", args={"layer": source_layer, "where": where},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


# ---------- stats ----------


def stats(
    session: Session, layer: str,
    columns: list[str] | None = None,
    group_by: list[str] | None = None,
    limit: int = 100,
) -> str:
    """Return a markdown table summarizing `columns` (numeric → count/min/avg/max,
    string → count/distinct_count), optionally grouped by `group_by`."""
    meta = _require_layer(session, layer)
    geom_col = meta.attributes.get("__geom_col__") or ""
    schema = {k: v for k, v in meta.attributes.items() if not k.startswith("__") and k != geom_col}

    # Derive default columns: all numeric columns if none given
    if columns is None:
        columns = [c for c, t in schema.items() if is_numeric_sql_type(t)]
    for c in (columns or []) + (group_by or []):
        if c not in schema:
            raise OpError(f"column '{c}' not in layer '{layer}'")

    select_bits: list[str] = []
    headers: list[str] = []
    if group_by:
        for c in group_by:
            select_bits.append(_quote_ident(c))
            headers.append(c)
    select_bits.append("COUNT(*) AS n")
    headers.append("n")
    for c in columns or []:
        qc = _quote_ident(c)
        if is_numeric_sql_type(schema[c]):
            select_bits += [
                f"MIN({qc}) AS {_quote_ident(c + '_min')}",
                f"AVG({qc}) AS {_quote_ident(c + '_avg')}",
                f"MAX({qc}) AS {_quote_ident(c + '_max')}",
                f"COUNT({qc}) AS {_quote_ident(c + '_n')}",
            ]
            headers += [f"{c}_min", f"{c}_avg", f"{c}_max", f"{c}_n"]
        else:
            select_bits += [
                f"COUNT({qc}) AS {_quote_ident(c + '_n')}",
                f"COUNT(DISTINCT {qc}) AS {_quote_ident(c + '_distinct')}",
            ]
            headers += [f"{c}_n", f"{c}_distinct"]

    group_sql = ""
    order_sql = ""
    if group_by:
        group_sql = " GROUP BY " + ", ".join(_quote_ident(c) for c in group_by)
        order_sql = " ORDER BY n DESC"
    sql = (
        f"SELECT {', '.join(select_bits)} FROM {_quote_ident(layer)}"
        f"{group_sql}{order_sql} LIMIT {int(limit)}"
    )
    rows = session.conn.execute(sql).fetchall()
    col_names = [d[0] for d in session.conn.description]
    md = ["| " + " | ".join(col_names) + " |", "|" + "|".join(["---"] * len(col_names)) + "|"]
    for r in rows:
        md.append("| " + " | ".join(_fmt_cell(v) for v in r) + " |")
    session.log(Operation(
        tool="stats",
        args={"layer": layer, "columns": columns, "group_by": group_by},
        result_layer=None, summary=f"{len(rows)} rows", at=datetime.utcnow(),
    ))
    return f"Stats for `{layer}` ({meta.feature_count} features)\n\n" + "\n".join(md)


# ---------- frequencies ----------


def frequencies(
    session: Session, layer: str, column: str, limit: int = 50,
) -> dict:
    """Return the top value counts for `column` on `layer`, ordered by
    frequency descending. Cheap wrapper around GROUP BY + COUNT — included
    as a first-class op because "what values are in this column and how
    often" is one of the most common follow-up questions after load().

    Args:
        layer: session layer.
        column: column name to group on.
        limit: max distinct values to return (default 50).

    Returns:
        {layer, column, n_distinct, n_total, n_null, rows: [{value, count}, ...]}
    """
    meta = _require_layer(session, layer)
    declared = [c for c in meta.attributes if not c.startswith("__")]
    if column not in declared:
        raise OpError(
            f"column '{column}' not on '{layer}'. Available: {declared}"
        )
    qlayer = _quote_ident(layer)
    qcol = _quote_ident(column)
    limit = max(1, min(int(limit), 1000))
    try:
        totals = session.conn.execute(
            f"SELECT COUNT(*), COUNT(*) - COUNT({qcol}), COUNT(DISTINCT {qcol}) "
            f"FROM {qlayer}"
        ).fetchone()
        n_total = int(totals[0])
        n_null = int(totals[1])
        n_distinct = int(totals[2])
        rows_raw = session.conn.execute(
            f"SELECT {qcol} AS value, COUNT(*) AS count FROM {qlayer} "
            f"GROUP BY {qcol} ORDER BY count DESC, value NULLS LAST LIMIT {limit}"
        ).fetchall()
    except Exception as e:
        raise OpError(f"frequencies failed: {type(e).__name__}: {e}")
    rows = [{"value": v, "count": int(c)} for v, c in rows_raw]
    out = {
        "layer": layer,
        "column": column,
        "n_total": n_total,
        "n_null": n_null,
        "n_distinct": n_distinct,
        "rows": rows,
    }
    if n_distinct > limit:
        out["truncated"] = True
        out["hint"] = (
            f"showing top {limit} of {n_distinct} distinct values; raise "
            f"`limit` (max 1000) or add a WHERE via filter() first."
        )
    return out


# ---------- batch_iterate ----------

BATCH_ITERATE_MAX = 500
BATCH_ITERATE_DEFAULT = 200


def batch_iterate(
    session: Session, layer: str,
    columns: list[str] | None = None,
    batch_size: int | None = None,
    cursor: str | None = None,
    where: str | None = None,
) -> dict:
    """Yield a batch of rows from `layer` with a resumable cursor. Use this to
    iterate over layers too large for a single inspect call. The LLM typically
    pairs it with `annotate` to classify every feature.

    On first call: pass `layer` + `columns` (+ optional `where`, `batch_size`).
    Response carries a `cursor` id. On subsequent calls, pass `cursor=...`.
    The batch_size you set on the first call is **remembered across continuation
    calls** (200 if unset). You can override it per-call by passing an explicit
    `batch_size` on the cursor call.

    When the cursor is exhausted, `next_cursor` in the response is null.
    """
    # Lazy import to avoid any circular dependency with sql.py (which
    # query.py does not import directly — classify uses add_field from
    # fields.py, and batch_iterate's where validation calls _validate_sql).
    from .sql import _validate_sql

    if cursor:
        state = session.cursors.get(cursor)
        if state is None:
            raise OpError(f"unknown cursor '{cursor}'. It may have expired or been consumed.")
        layer = state["layer"]
        columns = state["columns"]
        where = state["where"]
        # Honor explicit override, otherwise reuse the initial size.
        effective_batch_size = int(batch_size) if batch_size is not None else state["batch_size"]
        state["batch_size"] = effective_batch_size
        batch_size = max(1, min(effective_batch_size, BATCH_ITERATE_MAX))
    else:
        effective_batch_size = int(batch_size) if batch_size is not None else BATCH_ITERATE_DEFAULT
        batch_size = max(1, min(effective_batch_size, BATCH_ITERATE_MAX))
        meta = _require_layer(session, layer)
        declared = [c for c in meta.attributes if not c.startswith("__")]
        if not columns:
            # Default: all non-geometry columns.
            geom_col = meta.attributes.get("__geom_col__") or ""
            columns = [c for c in declared if c != geom_col]
        for c in columns:
            if c != "rowid" and c not in declared:
                raise OpError(f"unknown column '{c}' in layer '{layer}'. Available: {declared + ['rowid']}")
        if where:
            _assert_predicate(where, "where")
            _validate_sql(f"SELECT 1 FROM _t WHERE ({where})")
        import secrets as _secrets
        cursor = _secrets.token_urlsafe(12)
        state = {"layer": layer, "columns": columns, "where": where,
                 "offset": 0, "batch_size": batch_size}
        session.cursors[cursor] = state

    qlayer = _quote_ident(layer)
    # Always include rowid so annotate by rowid is straightforward.
    select_cols = ["rowid AS rowid"] + [_quote_ident(c) for c in columns if c != "rowid"]
    where_sql = f" WHERE {where}" if where else ""
    sql = (
        f"SELECT {', '.join(select_cols)} FROM {qlayer}{where_sql} "
        f"ORDER BY rowid LIMIT {batch_size} OFFSET {int(state['offset'])}"
    )
    try:
        rows = session.conn.execute(sql).fetchall()
        col_names = [d[0] for d in session.conn.description]
    except Exception as e:
        raise OpError(f"batch_iterate failed: {type(e).__name__}: {e}")

    returned_count = int(session.conn.execute(
        f"SELECT COUNT(*) FROM {qlayer}{where_sql}"
    ).fetchone()[0])

    # Advance offset.
    state["offset"] += len(rows)
    exhausted = len(rows) < batch_size
    if exhausted:
        # Free the cursor.
        session.cursors.pop(cursor, None)
        next_cursor = None
    else:
        next_cursor = cursor
    return {
        "layer": layer,
        "columns": col_names,
        "rows": [list(r) for r in rows],
        "batch_size": batch_size,
        "returned": len(rows),
        "total_matching": returned_count,
        "offset_after_batch": state["offset"],
        "next_cursor": next_cursor,
        "exhausted": exhausted,
    }


# ---------- macro ops: top_n / baseline_stats / classify ----------


def top_n(
    session: Session, layer: str, by: str,
    n: int = 10, ascending: bool = False,
    result_name: str | None = None,
) -> LayerMeta:
    """filter + ORDER BY + LIMIT in one call. Produces a new layer with the
    top (or bottom) `n` rows of `layer` sorted by the `by` expression.

    Args:
        layer: source layer.
        by: SQL ordering expression. Either a bare expression
            (`"population"`, `"ST_Area(geom)"`) with direction set via
            `ascending`, OR an expression with a trailing `ASC`/`DESC`
            (optionally followed by `NULLS FIRST`/`NULLS LAST`). If
            the trailing direction is present it wins over `ascending`.
        n: row cap (default 10).
        ascending: True → smallest first. Default False (largest first).
            Ignored when `by` already carries a direction.
        result_name: optional layer name; defaults to "<layer>_top<n>".

    Provenance inherits from `layer`.
    """
    meta = _require_layer(session, layer)
    expr, embedded_direction = _split_order_direction(by)
    _assert_single_sql_expression(expr, "by")
    new_name = session.unique_layer_name(result_name or f"{layer}_top{n}")
    if embedded_direction:
        direction_clause = embedded_direction
    else:
        direction_clause = ("ASC" if ascending else "DESC") + " NULLS LAST"
    sql = (
        f"CREATE TABLE {_quote_ident(new_name)} AS "
        f"SELECT * FROM {_quote_ident(layer)} "
        f"ORDER BY ({expr}) {direction_clause} LIMIT {int(n)}"
    )
    try:
        session.conn.execute(sql)
    except Exception as e:
        raise OpError(f"top_n failed: {type(e).__name__}: {e}")
    out = _register_result(session, new_name, [layer], created_by="top_n")
    session.log(Operation(
        tool="top_n",
        args={"layer": layer, "by": by, "n": n, "ascending": ascending,
              "result_name": new_name},
        result_layer=new_name, summary=f"top {n} of {layer} by {by}",
        at=datetime.utcnow(),
    ))
    return out


def baseline_stats(
    session: Session, layer: str, expression: str,
    group_by: list[str] | None = None,
) -> dict:
    """Compute baseline descriptive statistics for a SQL expression across
    an entire layer. Returns count / mean / median / p25 / p75 / min / max
    / stddev, optionally grouped. Intended for "compute the city-wide
    median income as a baseline for comparing a subset" — saves a
    hand-written CTE every time.
    """
    _require_layer(session, layer)
    _assert_single_sql_expression(expression, "expression")
    group_cols_sql = ""
    group_select = ""
    group_by_sql = ""
    if group_by:
        for g in group_by:
            _assert_single_sql_expression(g, "group_by")
        group_select = ", ".join(_quote_ident(g) for g in group_by) + ", "
        group_by_sql = "GROUP BY " + ", ".join(_quote_ident(g) for g in group_by)

    sql = (
        f"SELECT {group_select}"
        f"  COUNT(*) FILTER (WHERE ({expression}) IS NOT NULL) AS n,"
        f"  AVG(({expression}))                                  AS mean,"
        f"  MEDIAN(({expression}))                               AS median,"
        f"  QUANTILE_CONT(({expression}), 0.25)                  AS p25,"
        f"  QUANTILE_CONT(({expression}), 0.75)                  AS p75,"
        f"  MIN(({expression}))                                  AS min,"
        f"  MAX(({expression}))                                  AS max,"
        f"  STDDEV(({expression}))                               AS stddev"
        f" FROM {_quote_ident(layer)} {group_by_sql}"
        f" ORDER BY n DESC LIMIT 1000"
    )
    try:
        rows = session.conn.execute(sql).fetchall()
        cols = [d[0] for d in session.conn.description]
    except Exception as e:
        raise OpError(f"baseline_stats failed: {type(e).__name__}: {e}")
    groups = [dict(zip(cols, r)) for r in rows]
    payload = {
        "layer": layer,
        "expression": expression,
        "group_by": group_by,
        "n_groups": len(groups),
    }
    if not group_by and groups:
        payload["stats"] = groups[0]  # single global row
    else:
        payload["groups"] = groups
    session.log(Operation(
        tool="baseline_stats",
        args={"layer": layer, "expression": expression, "group_by": group_by},
        result_layer=None,
        summary=f"{len(groups)} group(s)",
        at=datetime.utcnow(),
    ))
    return payload


def classify(
    session: Session, layer: str, name: str,
    rules: list[dict], default: str | None = None,
) -> dict:
    """Add a column to `layer` whose value is chosen from the first matching
    rule in `rules`. Each rule is `{"when": <sql predicate>, "then": <literal>}`.
    Essentially a CASE WHEN ... THEN ... ELSE ... END wrapped as add_field.

    Use this for the canonical urban-analysis pattern of labeling features
    based on N signals (e.g. 'gentrifying' | 'stable' | 'declining').
    Reversible inside a covering checkpoint; stores the compiled expression
    in the operation log for provenance.
    """
    # Lazy import — add_field lives in fields.py, which imports sql.py; we
    # avoid a top-level cycle by deferring.
    from .fields import add_field

    if not rules:
        raise OpError("classify requires at least one rule")
    parts = ["CASE"]
    for i, rule in enumerate(rules):
        when = rule.get("when")
        then = rule.get("then")
        if when is None or then is None:
            raise OpError(f"rule {i} missing 'when' or 'then'")
        _assert_single_sql_expression(when, f"rules[{i}].when")
        # `then` is a literal — embed as a SQL literal (string/number/bool).
        parts.append(f"WHEN ({when}) THEN {_sql_literal(then)}")
    if default is not None:
        parts.append(f"ELSE {_sql_literal(default)}")
    parts.append("END")
    expr = " ".join(parts)
    # add_field does the heavy lifting (snapshot, column add, update, log).
    out = add_field(session, layer, name, expr)
    out["rules_count"] = len(rules)
    out["default"] = default
    return out
