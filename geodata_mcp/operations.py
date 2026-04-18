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
        return f"unknown layer '{layer}'. Available: {list(session.layers)}"
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
