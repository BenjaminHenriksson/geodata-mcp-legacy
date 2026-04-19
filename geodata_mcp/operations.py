"""Phase 2 tool implementations: filter, spatial, stats, execute_sql, sources.

Provenance is inherited from parent layers automatically. Each result layer
records its parents in `LayerMeta.parent_layers` and the union of parents'
source references (deduped by dataset id) in `LayerMeta.provenance`.
"""
from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime
from typing import Iterable

import duckdb
import sqlglot
from sqlglot import expressions as sqlexp

from .loader import (
    LoadError as OpError,
    MAX_FEATURES_PER_LOAD,
    _bbox_from_table,
    _column_schema,
    _detect_geometry_column,
    _quote_ident,
    _sql_str,
)
from .session import LayerMeta, Operation, Session, SourceRef


# ---------- provenance / meta helpers ----------


def _merge_provenance(*layers: LayerMeta) -> list[SourceRef]:
    seen: dict[tuple, SourceRef] = {}
    for m in layers:
        for s in m.provenance:
            key = (s.dataset_id, s.source_name, s.file_path, s.llm_sourced)
            seen.setdefault(key, s)
    return list(seen.values())


def _probe_geom_type(session: Session, table: str, geom_col: str) -> str | None:
    """Ask DuckDB what geometry types actually live in the result table.
    Returns 'Polygon' / 'LineString' / 'Point' / 'MultiPolygon' / 'GeometryCollection'
    etc., matching ST_GeometryType output. Returns None on no rows / errors."""
    try:
        rows = session.conn.execute(
            f"SELECT DISTINCT ST_GeometryType({_quote_ident(geom_col)}) "
            f"FROM {_quote_ident(table)} WHERE {_quote_ident(geom_col)} IS NOT NULL LIMIT 5"
        ).fetchall()
    except Exception:
        return None
    types = [r[0] for r in rows if r[0]]
    if not types:
        return None
    if len(types) == 1:
        return types[0]
    # Mixed types — report the mix so metadata doesn't silently lie.
    return "|".join(sorted(types))


def _register_result(
    session: Session, new_name: str, parents: list[str], created_by: str,
) -> LayerMeta:
    schema = _column_schema(session.conn, new_name)
    geom_col = _detect_geometry_column(schema)
    bbox = _bbox_from_table(session.conn, new_name, geom_col) if geom_col else None
    n = session.conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(new_name)}").fetchone()[0]
    parent_metas = [session.layers[p] for p in parents if p in session.layers]

    # Re-probe geometry type from the actual materialized rows, not the parent.
    # Inheriting from a_layer is wrong when the operation changes geometry kind
    # (polygon × point intersection → point, dissolve → MultiPolygon, etc.).
    geom_type = None
    if geom_col:
        geom_type = _probe_geom_type(session, new_name, geom_col)
    if geom_type is None and parent_metas:
        geom_type = parent_metas[0].geometry_type

    meta = LayerMeta(
        name=new_name, feature_count=int(n),
        geometry_type=geom_type,
        bbox=bbox, attributes=schema,
        created_by=created_by, created_at=datetime.utcnow(),
        provenance=_merge_provenance(*parent_metas),
        parent_layers=parents,
    )
    meta.attributes["__geom_col__"] = geom_col or ""
    session.register(meta)
    return meta


def _require_layer(session: Session, name: str) -> LayerMeta:
    m = session.layers.get(name)
    if m is None:
        raise OpError(f"unknown layer '{name}'. Available: {list(session.layers)}")
    return m


def _geom_col(meta: LayerMeta) -> str:
    g = meta.attributes.get("__geom_col__") or ""
    if not g:
        raise OpError(f"layer '{meta.name}' has no geometry")
    return g


def _no_semicolon(s: str, arg: str) -> None:
    if ";" in s:
        raise OpError(f"`{arg}` may not contain ';'")


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


# ---------- spatial ops ----------


def _clip_geom_expr(session: Session, by_layer: str) -> tuple[str, str]:
    """Build a SQL scalar subquery that yields the union of `by_layer`'s geometry
    as a single (Multi)Polygon. Also returns the geom column name for reference."""
    m = _require_layer(session, by_layer)
    g = _geom_col(m)
    expr = (f"(SELECT ST_Union_Agg({_quote_ident(g)}) "
            f"FROM {_quote_ident(by_layer)})")
    return expr, g


def spatial_clip(
    session: Session, source_layer: str, by_layer: str,
    *, result_name: str | None = None,
) -> LayerMeta:
    src = _require_layer(session, source_layer)
    by = _require_layer(session, by_layer)
    src_g = _geom_col(src)
    clip_expr, _ = _clip_geom_expr(session, by_layer)

    new_name = session.unique_layer_name(result_name or f"{source_layer}_clipped")
    cols = [c for c in src.attributes if not c.startswith("__") and c != src_g]
    attr_select = ", ".join(f"s.{_quote_ident(c)}" for c in cols)
    sql = (
        f"CREATE TABLE {_quote_ident(new_name)} AS "
        f"SELECT {attr_select + ',' if attr_select else ''} "
        f"ST_Intersection(s.{_quote_ident(src_g)}, {clip_expr}) AS {_quote_ident(src_g)} "
        f"FROM {_quote_ident(source_layer)} s "
        f"WHERE ST_Intersects(s.{_quote_ident(src_g)}, {clip_expr})"
    )
    session.conn.execute(sql)
    meta = _register_result(session, new_name, [source_layer, by_layer], created_by="spatial.clip")
    session.log(Operation(
        tool="spatial.clip", args={"layer": source_layer, "by_layer": by_layer},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


def spatial_buffer(
    session: Session, source_layer: str, distance_m: float,
    *, result_name: str | None = None,
) -> LayerMeta:
    src = _require_layer(session, source_layer)
    src_g = _geom_col(src)
    new_name = session.unique_layer_name(result_name or f"{source_layer}_buffer{int(distance_m)}")
    cols = [c for c in src.attributes if not c.startswith("__") and c != src_g]
    attr_select = ", ".join(_quote_ident(c) for c in cols)
    sql = (
        f"CREATE TABLE {_quote_ident(new_name)} AS "
        f"SELECT {attr_select + ',' if attr_select else ''} "
        f"ST_Buffer({_quote_ident(src_g)}, {float(distance_m)}) AS {_quote_ident(src_g)} "
        f"FROM {_quote_ident(source_layer)}"
    )
    session.conn.execute(sql)
    meta = _register_result(session, new_name, [source_layer], created_by="spatial.buffer")
    session.log(Operation(
        tool="spatial.buffer", args={"layer": source_layer, "distance_m": distance_m},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


def spatial_centroid(
    session: Session, source_layer: str,
    *, result_name: str | None = None,
) -> LayerMeta:
    src = _require_layer(session, source_layer)
    src_g = _geom_col(src)
    new_name = session.unique_layer_name(result_name or f"{source_layer}_centroid")
    cols = [c for c in src.attributes if not c.startswith("__") and c != src_g]
    attr_select = ", ".join(_quote_ident(c) for c in cols)
    sql = (
        f"CREATE TABLE {_quote_ident(new_name)} AS "
        f"SELECT {attr_select + ',' if attr_select else ''} "
        f"ST_Centroid({_quote_ident(src_g)}) AS {_quote_ident(src_g)} "
        f"FROM {_quote_ident(source_layer)}"
    )
    session.conn.execute(sql)
    meta = _register_result(session, new_name, [source_layer], created_by="spatial.centroid")
    session.log(Operation(
        tool="spatial.centroid", args={"layer": source_layer},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


def spatial_dissolve(
    session: Session, source_layer: str,
    by_columns: list[str] | None = None,
    *, result_name: str | None = None,
) -> LayerMeta:
    """Union geometries, optionally grouped by attribute columns.

    With no by_columns: emits 1 feature — the union of all.
    With by_columns: emits 1 feature per distinct combination of those columns.
    """
    src = _require_layer(session, source_layer)
    src_g = _geom_col(src)
    new_name = session.unique_layer_name(result_name or f"{source_layer}_dissolved")
    if by_columns:
        for c in by_columns:
            if c not in src.attributes:
                raise OpError(f"column '{c}' not in layer '{source_layer}'")
        group_cols = ", ".join(_quote_ident(c) for c in by_columns)
        select_cols = ", ".join(_quote_ident(c) for c in by_columns)
        sql = (
            f"CREATE TABLE {_quote_ident(new_name)} AS "
            f"SELECT {select_cols}, "
            f"       ST_Union_Agg({_quote_ident(src_g)}) AS {_quote_ident(src_g)}, "
            f"       COUNT(*) AS feature_count "
            f"FROM {_quote_ident(source_layer)} "
            f"GROUP BY {group_cols}"
        )
    else:
        sql = (
            f"CREATE TABLE {_quote_ident(new_name)} AS "
            f"SELECT COUNT(*) AS feature_count, "
            f"       ST_Union_Agg({_quote_ident(src_g)}) AS {_quote_ident(src_g)} "
            f"FROM {_quote_ident(source_layer)}"
        )
    session.conn.execute(sql)
    meta = _register_result(session, new_name, [source_layer], created_by="spatial.dissolve")
    session.log(Operation(
        tool="spatial.dissolve",
        args={"layer": source_layer, "by_columns": by_columns},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


def spatial_convex_hull(
    session: Session, source_layer: str,
    *, aggregate: bool = False, result_name: str | None = None,
) -> LayerMeta:
    """Per-feature convex hull by default; with aggregate=True, emit a single
    feature: the convex hull of the union of all geometries."""
    src = _require_layer(session, source_layer)
    src_g = _geom_col(src)
    new_name = session.unique_layer_name(result_name or f"{source_layer}_hull")
    if aggregate:
        sql = (
            f"CREATE TABLE {_quote_ident(new_name)} AS "
            f"SELECT COUNT(*) AS source_feature_count, "
            f"       ST_ConvexHull(ST_Union_Agg({_quote_ident(src_g)})) AS {_quote_ident(src_g)} "
            f"FROM {_quote_ident(source_layer)}"
        )
    else:
        cols = [c for c in src.attributes if not c.startswith("__") and c != src_g]
        attr_select = ", ".join(_quote_ident(c) for c in cols)
        sql = (
            f"CREATE TABLE {_quote_ident(new_name)} AS "
            f"SELECT {attr_select + ',' if attr_select else ''} "
            f"ST_ConvexHull({_quote_ident(src_g)}) AS {_quote_ident(src_g)} "
            f"FROM {_quote_ident(source_layer)}"
        )
    session.conn.execute(sql)
    meta = _register_result(session, new_name, [source_layer], created_by="spatial.convex_hull")
    session.log(Operation(
        tool="spatial.convex_hull",
        args={"layer": source_layer, "aggregate": aggregate},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


def spatial_intersect(
    session: Session, a_layer: str, b_layer: str,
    *, result_name: str | None = None,
) -> LayerMeta:
    """**Geometric** intersection (overlay): for every pair (a, b) where the
    geometries intersect, emit one row with a's attributes (prefixed `a_`),
    b's attributes (prefixed `b_`), and the geometry = `ST_Intersection(a, b)`.

    NOTE: if you want "rows of A that intersect anything in B" with A's
    geometry unchanged, use `spatial(operation='select_by_location', ...)`
    instead — that's the more common spatial-join pattern.
    """
    a = _require_layer(session, a_layer)
    b = _require_layer(session, b_layer)
    a_g, b_g = _geom_col(a), _geom_col(b)
    new_name = session.unique_layer_name(result_name or f"{a_layer}_x_{b_layer}")
    a_cols = [c for c in a.attributes if not c.startswith("__") and c != a_g]
    b_cols = [c for c in b.attributes if not c.startswith("__") and c != b_g]
    a_select = ", ".join(f"a.{_quote_ident(c)} AS {_quote_ident('a_' + c)}" for c in a_cols)
    b_select = ", ".join(f"b.{_quote_ident(c)} AS {_quote_ident('b_' + c)}" for c in b_cols)
    geom_col_out = "geom"
    sql = (
        f"CREATE TABLE {_quote_ident(new_name)} AS "
        f"SELECT {a_select}{',' if a_select else ''} "
        f"       {b_select}{',' if b_select else ''} "
        f"       ST_Intersection(a.{_quote_ident(a_g)}, b.{_quote_ident(b_g)}) AS {_quote_ident(geom_col_out)} "
        f"FROM {_quote_ident(a_layer)} a "
        f"JOIN {_quote_ident(b_layer)} b "
        f"  ON ST_Intersects(a.{_quote_ident(a_g)}, b.{_quote_ident(b_g)}) "
        f"WHERE NOT ST_IsEmpty(ST_Intersection(a.{_quote_ident(a_g)}, b.{_quote_ident(b_g)}))"
    )
    session.conn.execute(sql)
    meta = _register_result(session, new_name, [a_layer, b_layer], created_by="spatial.intersect")
    session.log(Operation(
        tool="spatial.intersect",
        args={"a_layer": a_layer, "b_layer": b_layer},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


def spatial_select_by_location(
    session: Session, source_layer: str, by_layer: str,
    *, predicate: str = "intersects", distance_m: float | None = None,
    result_name: str | None = None,
) -> LayerMeta:
    """Select features of `source_layer` whose geometry relates to ANY feature
    in `by_layer` by the given predicate. Source geometry and attributes are
    preserved unchanged — this is the spatial equivalent of a `WHERE` clause.

    Predicates:
      - intersects: ST_Intersects (default)  — touch, overlap, contain, equal
      - within:     ST_Within                 — source fully inside something in by_layer
      - contains:   ST_Contains               — source fully contains something in by_layer
      - dwithin:    ST_DWithin with `distance_m` metres (EPSG:3011)

    This is what you usually want for "give me buildings in this district"
    or "give me the DeSO containing this point". Distinct from
    `spatial(operation='intersect')`, which returns the **geometric**
    intersection (A ∩ B) and typically changes geometry kind.
    """
    src = _require_layer(session, source_layer)
    by = _require_layer(session, by_layer)
    src_g = _geom_col(src)
    by_g = _geom_col(by)

    pred = predicate.lower()
    if pred == "dwithin":
        if distance_m is None:
            raise OpError("predicate='dwithin' requires distance_m")
        cond = (f"ST_DWithin(s.{_quote_ident(src_g)}, b.{_quote_ident(by_g)}, "
                f"{float(distance_m)})")
    elif pred == "intersects":
        cond = f"ST_Intersects(s.{_quote_ident(src_g)}, b.{_quote_ident(by_g)})"
    elif pred == "within":
        cond = f"ST_Within(s.{_quote_ident(src_g)}, b.{_quote_ident(by_g)})"
    elif pred == "contains":
        cond = f"ST_Contains(s.{_quote_ident(src_g)}, b.{_quote_ident(by_g)})"
    else:
        raise OpError(
            f"unsupported predicate '{predicate}'. "
            "Supported: intersects, within, contains, dwithin."
        )

    new_name = session.unique_layer_name(
        result_name or f"{source_layer}_{pred}_{by_layer}"
    )
    qnew = _quote_ident(new_name)
    qsrc = _quote_ident(source_layer)
    qby = _quote_ident(by_layer)
    sql = (
        f"CREATE TABLE {qnew} AS "
        f"SELECT DISTINCT s.* FROM {qsrc} s "
        f"WHERE EXISTS (SELECT 1 FROM {qby} b WHERE {cond})"
    )
    session.conn.execute(sql)
    meta = _register_result(
        session, new_name, [source_layer, by_layer],
        created_by=f"spatial.select_by_location({pred})",
    )
    session.log(Operation(
        tool="spatial.select_by_location",
        args={"layer": source_layer, "by_layer": by_layer,
              "predicate": pred, "distance_m": distance_m},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


SPATIAL_OPS = {
    "clip": ("spatial_clip", ("layer", "by_layer")),
    "buffer": ("spatial_buffer", ("layer", "distance_m")),
    "centroid": ("spatial_centroid", ("layer",)),
    "dissolve": ("spatial_dissolve", ("layer", "by_columns?")),
    "convex_hull": ("spatial_convex_hull", ("layer", "aggregate?")),
    "intersect": ("spatial_intersect", ("a_layer", "b_layer")),
    "select_by_location": ("spatial_select_by_location",
                            ("layer", "by_layer", "predicate", "distance_m?")),
}


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
    numeric_types = {"INTEGER", "BIGINT", "DOUBLE", "FLOAT", "REAL", "DECIMAL", "HUGEINT", "TINYINT", "SMALLINT"}
    def is_numeric(t: str) -> bool:
        return any(t.upper().startswith(n) for n in numeric_types)

    if columns is None:
        columns = [c for c, t in schema.items() if is_numeric(t)]
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
        if is_numeric(schema[c]):
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


def _fmt_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.3f}" if abs(v) < 1e6 else f"{v:.3e}"
    s = str(v).replace("\n", " ").replace("|", "\\|")
    return s if len(s) < 80 else s[:77] + "..."


# ---------- execute_sql (with sqlglot-validated read-only sandbox) ----------


_DISALLOWED = (
    sqlexp.Insert, sqlexp.Update, sqlexp.Delete, sqlexp.Create,
    sqlexp.Drop, sqlexp.Alter, sqlexp.Merge, sqlexp.Copy,
)

# DuckDB table/scalar functions that touch the filesystem, network, or
# extension loader. These are rejected inside execute_sql even though their
# statements (plain SELECT) would otherwise pass validation. Catalog data is
# reachable via session layers — there is no legitimate user-facing need for
# these in execute_sql.
_DISALLOWED_FUNCTIONS = {
    # File readers
    "read_csv", "read_csv_auto", "read_parquet", "parquet_scan",
    "read_json", "read_json_auto", "read_json_objects", "json_scan",
    "read_blob", "read_text",
    "read_xlsx", "read_excel",
    # File system enumeration
    "glob", "parquet_schema", "parquet_metadata",
    "csv_sniffer", "sniff_csv",
    # Extension + attach (mostly already blocked by statement-type check,
    # but belt-and-braces)
    "load_extension", "install_extension",
    "attach", "detach",
    # httpfs / S3 direct
    "httpfs_register_secret", "s3_register_secret",
    # Process / environment
    "current_schemas", "pg_listen", "pg_terminate_backend",
}

# URL schemes we never want to see in a string literal. http(s) is the main
# SSRF concern; file:// + s3:// + gs:// etc. are exfiltration/privilege paths.
_DISALLOWED_URL_PREFIXES = (
    "http://", "https://", "ftp://", "ftps://",
    "file://", "s3://", "gs://", "r2://",
    "azure://", "abfs://", "abfss://",
    "hf://", "gcs://", "oss://",
)

# Absolute paths outside the catalog data dir are rejected inside execute_sql
# strings. This catches read_text('/etc/hostname') even if the function name
# was obfuscated. The catalog data directory itself is never accessed
# directly from execute_sql — it's always via session layers — so blocking
# all absolute paths here is safe.
_ABS_PATH_PREFIXES = ("/etc", "/home", "/root", "/var", "/proc", "/sys",
                      "/boot", "/usr", "/opt", "/srv", "/run", "/tmp")

# Per-query caps to stop `repeat('a', 1e10)` / `generate_series(0, 1e10)`
# from chewing memory even within the 256 MB session cap.
_MAX_LITERAL_LARGE_INT = 10_000_000


class SqlError(OpError):
    pass


def _func_name(node: sqlexp.Expression) -> str | None:
    """Try hard to get the callable name out of a sqlglot expression node."""
    if isinstance(node, sqlexp.Anonymous):
        return (node.name or "").lower()
    if isinstance(node, sqlexp.Func):
        # Some Func subclasses (Sum, Count, ...) have a class-level name.
        name = getattr(type(node), "sql_name", None)
        if callable(name):
            try:
                return name().lower()
            except TypeError:
                pass
        return type(node).__name__.lower()
    return None


def _validate_sql(sql: str) -> sqlexp.Expression:
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.ParseError as e:
        raise SqlError(f"parse error: {e}")
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SqlError(f"expected exactly 1 statement, got {len(statements)}")
    stmt = statements[0]
    if not isinstance(stmt, (sqlexp.Select, sqlexp.Subquery, sqlexp.With, sqlexp.Union)):
        raise SqlError(f"only SELECT/WITH/UNION allowed; got {stmt.key}")

    for node in stmt.walk():
        # DDL / DML anywhere in the tree → reject
        if isinstance(node, _DISALLOWED):
            raise SqlError(f"disallowed expression: {type(node).__name__}")

        # Dangerous function calls by name
        name = _func_name(node)
        if name and name in _DISALLOWED_FUNCTIONS:
            raise SqlError(
                f"disallowed function: {name}. "
                "Catalog data is reachable via session layers (see search_data / load) — "
                "read_*/glob/scan/attach/load_extension are blocked in execute_sql."
            )

        # String literals that are URL or absolute-path references
        if isinstance(node, sqlexp.Literal) and node.is_string:
            raw = str(node.this)
            lower = raw.lower()
            if any(lower.startswith(p) for p in _DISALLOWED_URL_PREFIXES):
                raise SqlError(
                    f"disallowed URL literal (scheme blocked): {raw[:80]!r}"
                )
            if any(lower.startswith(p) for p in _ABS_PATH_PREFIXES):
                raise SqlError(
                    f"disallowed absolute-path literal in SQL: {raw[:80]!r}. "
                    "Access files via catalog layers, not raw paths."
                )

        # Cap generators / repeat-like constructs that can OOM the session.
        # `repeat('a', 1e10)` and `generate_series(0, 1e10)` get caught here
        # by literal inspection, before DuckDB allocates.
        if isinstance(node, sqlexp.Literal) and not node.is_string:
            try:
                n = int(float(str(node.this)))
            except Exception:
                n = 0
            if n > _MAX_LITERAL_LARGE_INT:
                raise SqlError(
                    f"numeric literal {n} exceeds cap {_MAX_LITERAL_LARGE_INT} — "
                    "prevents memory exhaustion via repeat/generate_series."
                )
    return stmt


EXECUTE_SQL_TIMEOUT_S = 30.0
EXECUTE_SQL_MARKDOWN_ROW_CAP = 50


_GEOM_COLUMN_NAMES = {"geom", "geometry", "sp_geometry", "shape", "the_geom"}


def _describe_result_schema(conn, sql: str) -> list[tuple[str, str]]:
    """Ask DuckDB for the result schema of the user's SELECT without
    materializing all rows. Returns [(col, type_str), ...]."""
    try:
        rows = conn.execute(f"DESCRIBE ({sql})").fetchall()
    except Exception:
        return []
    return [(r[0], r[1]) for r in rows]


def execute_sql(
    session: Session, sql: str,
    *, description: str = "", result_name: str | None = None,
    geometry_column: str | None = None,
) -> dict:
    """Run a validated read-only SELECT. If geometry is produced, materialize
    as a new layer; otherwise return up to 50 rows as markdown.

    Validation errors raise SqlError (caller labels 'sql_rejected').
    Execution errors raise OpError (caller labels 'sql_failed').
    """
    _validate_sql(sql)  # → SqlError on failure

    # Detect geometry by result schema. `DESCRIBE (<sql>)` in DuckDB returns
    # types like 'GEOMETRY' or 'GEOMETRY(Point)'; only BLOB columns sneak in
    # when the engine drops the tag during cross-table expressions.
    schema = _describe_result_schema(session.conn, sql)
    auto_geom_col = next(
        (c for c, t in schema if t.upper().startswith("GEOMETRY")),
        None,
    )
    blob_geom_candidate = next(
        (c for c, t in schema
         if t.upper() == "BLOB" and c.lower() in _GEOM_COLUMN_NAMES),
        None,
    )

    def _run_with_timeout(sql_to_run: str, fetch: bool):
        timer = threading.Timer(EXECUTE_SQL_TIMEOUT_S, session.conn.interrupt)
        timer.start()
        try:
            cur = session.conn.execute(sql_to_run)
            if fetch:
                result = cur.fetchmany(EXECUTE_SQL_MARKDOWN_ROW_CAP)
                cols = [d[0] for d in session.conn.description]
                return result, cols
            return None, None
        finally:
            timer.cancel()

    # Decide layer-vs-table. Precedence:
    #   1. explicit geometry_column (force layer mode, cast to GEOMETRY)
    #   2. auto-detected column with GEOMETRY type
    #   3. table mode (with a hint if a BLOB column looks like it should've been geometry)
    chosen_geom_col = geometry_column or auto_geom_col
    if chosen_geom_col:
        # If it's the explicit hint and a column by that name exists, cast it
        # to GEOMETRY (covers the BLOB-leaks case).
        exists = any(c == chosen_geom_col for c, _ in schema)
        if not exists:
            raise OpError(
                f"geometry_column '{chosen_geom_col}' not in result schema "
                f"({[c for c, _ in schema]})"
            )
        if geometry_column is not None and auto_geom_col != geometry_column:
            # Wrap so the stored column is of type GEOMETRY.
            other_cols = [c for c, _ in schema if c != chosen_geom_col]
            other_select = (", ".join(_quote_ident(c) for c in other_cols) + ", ") if other_cols else ""
            wrapped = (
                f"SELECT {other_select}"
                f"{_quote_ident(chosen_geom_col)}::GEOMETRY AS {_quote_ident(chosen_geom_col)} "
                f"FROM ({sql}) _s"
            )
        else:
            wrapped = sql

        new_name = session.unique_layer_name(result_name or "sql_result")
        cta = f"CREATE TABLE {_quote_ident(new_name)} AS {wrapped}"
        try:
            _run_with_timeout(cta, fetch=False)
        except Exception as e:
            raise OpError(f"{type(e).__name__}: {e}")
        meta = _register_result(session, new_name, [], created_by="execute_sql")
        session.log(Operation(
            tool="execute_sql",
            args={"sql": sql, "description": description,
                  "geometry_column": geometry_column},
            result_layer=new_name,
            summary=f"{meta.feature_count} features",
            at=datetime.utcnow(),
        ))
        return {
            "mode": "layer",
            "layer_name": meta.name,
            "feature_count": meta.feature_count,
            "geometry_type": meta.geometry_type,
            "description": description,
        }

    # Table mode
    try:
        result, cols = _run_with_timeout(sql, fetch=True)
    except Exception as e:
        raise OpError(f"{type(e).__name__}: {e}")
    md = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for r in result:
        md.append("| " + " | ".join(_fmt_cell(v) for v in r) + " |")
    session.log(Operation(
        tool="execute_sql",
        args={"sql": sql, "description": description},
        result_layer=None, summary=f"{len(result)} rows",
        at=datetime.utcnow(),
    ))
    payload = {
        "mode": "table",
        "rows": len(result),
        "capped_at": EXECUTE_SQL_MARKDOWN_ROW_CAP,
        "description": description,
        "markdown": "\n".join(md),
    }
    if len(result) >= EXECUTE_SQL_MARKDOWN_ROW_CAP:
        payload["truncated"] = True
        payload["warning"] = (
            f"result capped at {EXECUTE_SQL_MARKDOWN_ROW_CAP} rows — the full "
            "result set may be larger. To get all rows, either narrow the "
            "query, or re-run with `result_name='...'` to materialize as a "
            "full layer (then iterate via `batch_iterate` or export)."
        )
    if blob_geom_candidate:
        payload["geometry_hint"] = (
            f"Column '{blob_geom_candidate}' looks like geometry but was stored "
            f"as BLOB (common with cross-layer expressions). Re-run with "
            f"`geometry_column='{blob_geom_candidate}'` to force layer mode, "
            "or cast it explicitly in SQL, e.g. "
            f"`SELECT …, {blob_geom_candidate}::GEOMETRY AS geom FROM ({{your sql}})`."
        )
    return payload


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

    Args:
        name: Desired layer name (may be suffixed if it collides).
        data: List of dicts. Each dict = one row. All rows should share keys.
        source: Free-text description of where the LLM got this data (required
                for meaningful provenance). Stored as `llm_source_description`.
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

    # Register the arrow table so we can CREATE TABLE AS SELECT from it.
    tmp = "_create_layer_tmp"
    session.conn.register(tmp, table)
    try:
        if geometry_column:
            other_cols = [c for c in columns if c != geometry_column]
            attr_select = ", ".join(_quote_ident(c) for c in other_cols)
            src_crs = crs if crs.startswith("EPSG:") else f"EPSG:{crs}"
            sql = (
                f"CREATE TABLE {qnew} AS "
                f"SELECT {attr_select + ',' if attr_select else ''} "
                f"ST_Transform(ST_GeomFromText({_quote_ident(geometry_column)}), "
                f"'{src_crs}', 'EPSG:3011', true) AS geom "
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


# ---------- export (Phase 3) ----------


import secrets
import time
from pathlib import Path as _Path

EXPORT_ROOT = _Path(__file__).resolve().parents[1] / "data" / "exports"
EXPORT_TTL_S = 24 * 60 * 60   # 24 h

VALID_EXPORT_FORMATS = {"geojson", "gpkg", "csv", "parquet"}


def _purge_expired_exports() -> int:
    if not EXPORT_ROOT.exists():
        return 0
    now = time.time()
    removed = 0
    for token_dir in EXPORT_ROOT.iterdir():
        if not token_dir.is_dir():
            continue
        try:
            if now - token_dir.stat().st_mtime > EXPORT_TTL_S:
                for f in token_dir.iterdir():
                    f.unlink()
                token_dir.rmdir()
                removed += 1
        except OSError:
            pass
    return removed


def export_layer(
    session: Session, layer: str, fmt: str = "geojson",
) -> dict:
    """Write a layer to data/exports/<token>/<name>.<ext> and return its URL + size.

    Formats:
      geojson — EPSG:4326 GeoJSON FeatureCollection
      gpkg    — OGC GeoPackage in native EPSG:3011
      csv     — attributes only + geom serialized as WKT
      parquet — columnar, geometry as WKB
    """
    fmt = fmt.lower()
    if fmt not in VALID_EXPORT_FORMATS:
        raise OpError(
            f"unsupported format '{fmt}'. Supported: {sorted(VALID_EXPORT_FORMATS)}"
        )
    meta = _require_layer(session, layer)
    geom_col = meta.attributes.get("__geom_col__") or ""

    _purge_expired_exports()
    token = secrets.token_urlsafe(16)
    dest_dir = EXPORT_ROOT / token
    dest_dir.mkdir(parents=True, exist_ok=True)

    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in layer)
    file_name = f"{safe_name}.{fmt}"
    dest = dest_dir / file_name

    qlayer = _quote_ident(layer)
    attr_cols = [c for c in meta.attributes
                 if not c.startswith("__") and c != geom_col]
    attr_select = ", ".join(_quote_ident(c) for c in attr_cols) or "1"

    if fmt == "geojson":
        if not geom_col:
            raise OpError(f"layer '{layer}' has no geometry; cannot export as GeoJSON")
        sql = (
            f"COPY (SELECT {attr_select + ',' if attr_cols else ''} "
            f"ST_Transform({_quote_ident(geom_col)}, 'EPSG:3011', 'EPSG:4326', true) "
            f"AS geom FROM {qlayer}) "
            f"TO '{dest}' (FORMAT GDAL, DRIVER 'GeoJSON', SRS 'EPSG:4326')"
        )
    elif fmt == "gpkg":
        if not geom_col:
            raise OpError(f"layer '{layer}' has no geometry; cannot export as GPKG")
        sql = (
            f"COPY (SELECT * FROM {qlayer}) "
            f"TO '{dest}' (FORMAT GDAL, DRIVER 'GPKG', "
            f"LAYER_NAME '{safe_name}', SRS 'EPSG:3011')"
        )
    elif fmt == "csv":
        if geom_col:
            sel = (f"SELECT {attr_select + ',' if attr_cols else ''} "
                   f"ST_AsText({_quote_ident(geom_col)}) AS geom_wkt "
                   f"FROM {qlayer}")
        else:
            sel = f"SELECT * FROM {qlayer}"
        sql = f"COPY ({sel}) TO '{dest}' (HEADER, DELIMITER ',')"
    else:  # parquet
        sql = f"COPY (SELECT * FROM {qlayer}) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)"

    try:
        session.conn.execute(sql)
    except Exception as e:
        raise OpError(f"export failed: {type(e).__name__}: {e}")

    size_bytes = dest.stat().st_size
    expires_at = time.time() + EXPORT_TTL_S
    url = f"/exports/{token}/{file_name}"

    session.log(Operation(
        tool="export",
        args={"layer": layer, "format": fmt},
        result_layer=None,
        summary=f"{fmt} {size_bytes} bytes",
        at=datetime.utcnow(),
    ))
    return {
        "layer": layer,
        "format": fmt,
        "url": url,
        "file_name": file_name,
        "size_bytes": size_bytes,
        "expires_at": datetime.utcfromtimestamp(expires_at).isoformat() + "Z",
        "provenance": [
            {"dataset_id": s.dataset_id, "source_name": s.source_name,
             "publisher": s.publisher, "license": s.license, "url": s.url,
             "retrieved": s.retrieved, "llm_sourced": s.llm_sourced,
             "llm_source_description": s.llm_source_description}
            for s in meta.provenance
        ],
    }


# ---------- sources / provenance walker ----------


def _ancestry(session: Session, layer: str) -> list[str]:
    """Return layers in BFS order from `layer` back through parents. `layer` first."""
    seen = {layer}
    order = [layer]
    queue = [layer]
    while queue:
        cur = queue.pop(0)
        m = session.layers.get(cur)
        if m is None:
            continue
        for p in m.parent_layers:
            if p in seen:
                continue
            seen.add(p)
            order.append(p)
            queue.append(p)
    return order


def sources(session: Session, layer: str | None = None) -> str:
    """Markdown report of the provenance chain and operations applied."""
    targets = [layer] if layer else list(session.layers)
    if layer and layer not in session.layers:
        return (f"[server-side error from geodata-mcp server] unknown layer "
                f"'{layer}' in this session. Available: {list(session.layers)}")
    out: list[str] = []
    for lname in targets:
        m = session.layers[lname]
        ancestors = _ancestry(session, lname)
        ops = [op for op in session.history if op.result_layer in ancestors]
        out.append(f"### Layer: `{lname}`  ({m.feature_count} features, created by `{m.created_by}`)")
        if m.parent_layers:
            out.append(f"Derived from: {', '.join(f'`{p}`' for p in m.parent_layers)}")
        out.append("")
        out.append("**Operations applied** (root → result):")
        for op in ops:
            arg_str = ", ".join(f"{k}={v!r}" for k, v in op.args.items() if v is not None)
            out.append(f"- `{op.tool}({arg_str})` → `{op.result_layer}`: {op.summary}")
        out.append("")
        out.append("**Sources** (deduplicated):")
        if not m.provenance:
            out.append("- (no source references)")
        for i, s in enumerate(m.provenance, 1):
            line = f"{i}. "
            if s.llm_sourced:
                line += f"**LLM-sourced**: {s.source_name}"
                if s.llm_source_description:
                    line += f" — _{s.llm_source_description}_"
            else:
                line += f"**{s.source_name}** (`{s.dataset_id}`)"
            out.append(line)
            if s.publisher: out.append(f"   - Publisher: {s.publisher}")
            if s.license: out.append(f"   - License: {s.license}")
            if s.url: out.append(f"   - URL: {s.url}")
            if s.file_path: out.append(f"   - File: `{s.file_path}`")
            if s.retrieved: out.append(f"   - Retrieved: {s.retrieved}")
        out.append("")
    return "\n".join(out)


# ======================================================================
# P1 additions: in-place field ops, annotate, list/drop/rename layers,
#               batch iteration, spatial point lookup, notes, checkpoints.
# ======================================================================


# ---------- checkpoint / rollback infrastructure ----------

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


# ---------- add_field / update_field / drop_field ----------

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
    """Expressions passed to add/update_field are inserted into CTAS/UPDATE
    SQL. We run them through the same validator that gates execute_sql
    (by embedding into a trivial SELECT) to catch file readers / URLs /
    abs-paths / big literals. Semicolons are flat-rejected."""
    if ";" in expr:
        raise OpError("expression cannot contain ';'")
    # Validate by embedding. Any issue the execute_sql validator would catch
    # is caught here too.
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
    meta = _require_layer(session, layer)
    if name not in meta.attributes or name.startswith("__"):
        raise OpError(f"column '{name}' not on '{layer}'. "
                      f"Available: {[c for c in meta.attributes if not c.startswith('__')]}")
    _sanity_check_expr(expr)
    if where:
        if ";" in where:
            raise OpError("`where` cannot contain ';'")
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
    """
    meta = _require_layer(session, layer)
    if not values:
        raise OpError("`values` must not be empty")
    if len(values) > ANNOTATE_CAP:
        raise OpError(f"too many annotations ({len(values)}); cap is {ANNOTATE_CAP}")

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
        "reversible": bool(covering),
        "covering_checkpoints": covering,
    }
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


# ---------- list_layers + set_notes ----------

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
    meta.notes = notes
    session.log(Operation(
        tool="set_notes", args={"layer": layer, "notes_len": len(notes)},
        result_layer=layer, summary=f"notes updated ({len(notes)} chars)",
        at=datetime.utcnow(),
    ))
    return {"layer": layer, "notes_chars": len(notes)}


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
            if ";" in where:
                raise OpError("`where` cannot contain ';'")
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


# ---------- inspect_location / inspect_locations / hide_layers ----------

def _inspect_one_point(
    session: Session, x: float, y: float, radius_m: float,
    targets: list[str], columns: list[str] | None, per_layer_limit: int,
) -> tuple[list[dict], list[str]]:
    """Run the per-layer ST_DWithin query for a single point. Returns
    (results, unknown_layers)."""
    results = []
    unknown = []
    for lname in targets:
        meta = session.layers.get(lname)
        if meta is None:
            unknown.append(lname)
            continue
        geom_col = meta.attributes.get("__geom_col__") or ""
        if not geom_col:
            continue
        qlayer = _quote_ident(lname)
        qgeom = _quote_ident(geom_col)
        declared = [c for c in meta.attributes
                    if not c.startswith("__") and c != geom_col]
        if columns:
            # Intersect requested cols with declared; silently drop unknown.
            attr_cols = [c for c in columns if c in declared]
        else:
            attr_cols = declared
        attr_select = ", ".join(_quote_ident(c) for c in attr_cols) or "NULL AS _"
        sql = (
            f"SELECT {attr_select}, "
            f"  ST_Distance({qgeom}, ST_Point(?, ?)) AS _dist_m "
            f"FROM {qlayer} "
            f"WHERE ST_DWithin({qgeom}, ST_Point(?, ?), ?) "
            f"ORDER BY _dist_m ASC "
            f"LIMIT {per_layer_limit}"
        )
        try:
            rows = session.conn.execute(
                sql, [x, y, x, y, radius_m]
            ).fetchall()
            col_names = [d[0] for d in session.conn.description]
        except Exception as e:
            results.append({"layer": lname, "error": f"{type(e).__name__}: {e}"})
            continue
        if not rows:
            continue
        hits = [dict(zip(col_names, r)) for r in rows]
        for h in hits:
            if "_dist_m" in h and h["_dist_m"] is not None:
                h["distance_m"] = round(float(h.pop("_dist_m")), 2)
            # Drop NULL attributes to keep payload small.
            for k in list(h.keys()):
                if h[k] is None:
                    h.pop(k)
        results.append({
            "layer": lname,
            "geometry_type": meta.geometry_type,
            "features": hits,
        })
    return results, unknown


def inspect_location(
    session: Session,
    x_3011: float,
    y_3011: float,
    radius_m: float = 100.0,
    layers: list[str] | None = None,
    columns: list[str] | None = None,
    per_layer_limit: int = 3,
) -> dict:
    """One-shot "what's here?" across multiple layers. See the tool wrapper
    for full description."""
    per_layer_limit = max(1, min(int(per_layer_limit), 25))
    radius_m = max(0.0, float(radius_m))

    geom_layers = [n for n, m in session.layers.items()
                   if m.attributes.get("__geom_col__")]
    if layers:
        targets = layers
    else:
        targets = geom_layers

    payload = {
        "query": {"x_3011": x_3011, "y_3011": y_3011, "radius_m": radius_m},
        "layers_considered": len(targets),
    }
    if not session.layers:
        payload.update({"results": [],
                        "hint": "no layers loaded in this session; call load() or load_many() first"})
        return payload
    if not geom_layers:
        payload.update({"results": [],
                        "hint": "no layers with geometry in this session"})
        return payload

    results, unknown = _inspect_one_point(
        session, x_3011, y_3011, radius_m,
        targets, columns, per_layer_limit,
    )
    session.log(Operation(
        tool="inspect_location",
        args={"x_3011": x_3011, "y_3011": y_3011, "radius_m": radius_m,
              "layers": targets},
        result_layer=None,
        summary=f"{sum(len(r.get('features', [])) for r in results)} features across {len(results)} layers",
        at=datetime.utcnow(),
    ))
    payload["results"] = results
    if unknown:
        payload["unknown_layers"] = unknown
        payload["warning"] = (
            f"layers {unknown} not loaded in this session — they were skipped. "
            "Call load() / load_many() first."
        )
    if all(not r.get("features") for r in results) and not unknown:
        payload["hint"] = (
            f"no features within {radius_m} m of ({x_3011}, {y_3011}) across "
            f"{len(results)} loaded layers. Try a larger radius, a different "
            "point, or load more layers."
        )
    return payload


def inspect_locations(
    session: Session,
    points: list[dict],
    radius_m: float = 100.0,
    layers: list[str] | None = None,
    columns: list[str] | None = None,
    per_layer_limit: int = 3,
) -> dict:
    """Batch spatial lookup: for each point in `points`, return the same
    per-layer nearest-feature hits as `inspect_location`."""
    per_layer_limit = max(1, min(int(per_layer_limit), 25))
    radius_m = max(0.0, float(radius_m))

    geom_layers = [n for n, m in session.layers.items()
                   if m.attributes.get("__geom_col__")]
    targets = layers if layers else geom_layers

    overall_unknown: set[str] = set()
    out_points = []
    for i, p in enumerate(points):
        x = p.get("x_3011")
        y = p.get("y_3011")
        pid = p.get("id", i)
        if x is None or y is None:
            out_points.append({"id": pid, "error": "missing x_3011/y_3011"})
            continue
        results, unknown = _inspect_one_point(
            session, float(x), float(y), radius_m,
            targets, columns, per_layer_limit,
        )
        overall_unknown.update(unknown)
        out_points.append({
            "id": pid,
            "x_3011": x, "y_3011": y,
            "results": results,
        })
    session.log(Operation(
        tool="inspect_locations",
        args={"n_points": len(points), "radius_m": radius_m,
              "layers": targets},
        result_layer=None,
        summary=f"{len(points)} points × {len(targets)} layers",
        at=datetime.utcnow(),
    ))
    payload = {
        "n_points": len(points),
        "layers_considered": len(targets),
        "points": out_points,
    }
    if overall_unknown:
        payload["unknown_layers"] = sorted(overall_unknown)
        payload["warning"] = (
            f"layers {sorted(overall_unknown)} not loaded — skipped for every point."
        )
    if not geom_layers:
        payload["hint"] = "no layers with geometry in this session"
    return payload


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
