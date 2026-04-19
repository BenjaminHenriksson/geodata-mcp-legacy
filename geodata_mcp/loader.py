"""Read normalized files into a session's DuckDB instance."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb

from .catalog import DatasetEntry
from .session import LayerMeta, Operation, Session


# Hard caps from plan. Geopackage geometries balloon in memory and choke the
# viewer; parquet tables are columnar + compact, so they're loaded in full.
MAX_FEATURES_PER_LOAD = 100_000
MAX_ROWS_PARQUET_LOAD = 10_000_000


class LoadError(Exception):
    pass


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _bbox_from_table(conn: duckdb.DuckDBPyConnection, table: str, geom_col: str) -> tuple[float, float, float, float] | None:
    try:
        row = conn.execute(
            f"SELECT ST_XMin(ST_Extent_Agg({_quote_ident(geom_col)})), "
            f"       ST_YMin(ST_Extent_Agg({_quote_ident(geom_col)})), "
            f"       ST_XMax(ST_Extent_Agg({_quote_ident(geom_col)})), "
            f"       ST_YMax(ST_Extent_Agg({_quote_ident(geom_col)})) "
            f"FROM {_quote_ident(table)}"
        ).fetchone()
        if row and all(x is not None for x in row):
            return tuple(float(x) for x in row)  # type: ignore
    except duckdb.Error:
        pass
    return None


def _column_schema(conn: duckdb.DuckDBPyConnection, table: str) -> dict[str, str]:
    rows = conn.execute(f"DESCRIBE {_quote_ident(table)}").fetchall()
    return {r[0]: r[1] for r in rows}


def _detect_geometry_column(schema: dict[str, str]) -> str | None:
    for col, typ in schema.items():
        if typ.upper().startswith("GEOMETRY"):
            return col
    return None


def _peek_schema(conn: duckdb.DuckDBPyConnection, src_expr: str) -> dict[str, str]:
    rows = conn.execute(f"DESCRIBE SELECT * FROM {src_expr}").fetchall()
    return {r[0]: r[1] for r in rows}


def _layer_geom_col(session: Session, layer: str) -> str:
    meta = session.layers.get(layer)
    if meta is None:
        raise LoadError(f"unknown layer '{layer}'")
    g = meta.attributes.get("__geom_col__") or ""
    if not g:
        raise LoadError(f"layer '{layer}' has no geometry column")
    return g


def load_dataset(
    session: Session,
    dataset: DatasetEntry,
    *,
    bbox_3011: tuple[float, float, float, float] | None = None,
    limit: int | None = None,
    layer_name: str | None = None,
    where: str | None = None,
    intersect_layer: str | None = None,
) -> LayerMeta:
    """Read a normalized dataset into the session as a DuckDB table.

    Filter options are AND-combined and applied at load time (before the feature cap):
      bbox_3011:        rectangular spatial filter in EPSG:3011
      where:            SQL WHERE clause on attributes (no semicolons)
      intersect_layer:  spatially restrict to features intersecting the geometry
                        of an already-loaded layer in this session (efficient
                        alternative to loading everything then clipping).
    """
    src = dataset.absolute_path()
    if not src.exists():
        raise LoadError(f"Dataset file missing: {dataset.file_path}")
    if where:
        # Parse-level check: reject only if the predicate is actually
        # multi-statement, allow literal ';' inside quoted strings.
        try:
            import sqlglot
            stmts = [s for s in sqlglot.parse(
                f"SELECT 1 FROM _t WHERE ({where})", read="duckdb"
            ) if s is not None]
        except Exception as e:
            raise LoadError(f"`where` is not a valid SQL predicate: {e}")
        if len(stmts) != 1:
            raise LoadError(
                f"`where` must be a single SQL predicate "
                f"(got {len(stmts)} statements)"
            )

    name = session.unique_layer_name(layer_name or dataset.id)
    qname = _quote_ident(name)

    if dataset.source_type == "geopackage":
        layer_arg = f", layer={_sql_str(dataset.layer)}" if dataset.layer else ""
        src_expr = f"ST_Read({_sql_str(str(src))}{layer_arg})"
    elif dataset.source_type == "parquet":
        src_expr = f"read_parquet({_sql_str(str(src))})"
    else:
        raise LoadError(f"Unsupported source_type: {dataset.source_type}")

    src_schema = _peek_schema(session.conn, src_expr)
    src_geom_col = _detect_geometry_column(src_schema)

    where_clauses: list[str] = []
    if bbox_3011 and src_geom_col:
        x1, y1, x2, y2 = bbox_3011
        where_clauses.append(
            f"ST_Intersects({_quote_ident(src_geom_col)}, "
            f"ST_MakeEnvelope({x1}, {y1}, {x2}, {y2}))"
        )
    if intersect_layer and src_geom_col:
        clip_geom_col = _layer_geom_col(session, intersect_layer)
        # Use the clip layer's bbox to pre-filter via MakeEnvelope (cheap, uses
        # GPKG/DuckDB rtree), then the exact intersect for correctness.
        clip_qname = _quote_ident(intersect_layer)
        where_clauses.append(
            f"ST_Intersects({_quote_ident(src_geom_col)}, "
            f"(SELECT ST_Union_Agg({_quote_ident(clip_geom_col)}) FROM {clip_qname}))"
        )
    if where:
        where_clauses.append(f"({where})")
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    is_parquet = dataset.source_type == "parquet"
    cap = MAX_ROWS_PARQUET_LOAD if is_parquet else MAX_FEATURES_PER_LOAD
    limit_sql = f" LIMIT {int(limit)}" if limit else f" LIMIT {cap + 1}"

    sql = f"CREATE TABLE {qname} AS SELECT * FROM {src_expr}{where_sql}{limit_sql}"
    session.conn.execute(sql)

    n = session.conn.execute(f"SELECT COUNT(*) FROM {qname}").fetchone()[0]
    truncated = n > cap
    if truncated:
        session.conn.execute(f"DROP TABLE {qname}")
        session.conn.execute(
            f"CREATE TABLE {qname} AS SELECT * FROM {src_expr}{where_sql} LIMIT {cap}"
        )
        n = cap

    schema = _column_schema(session.conn, name)
    geom_col = _detect_geometry_column(schema)
    bbox = _bbox_from_table(session.conn, name, geom_col) if geom_col else None

    parents: list[str] = []
    if intersect_layer:
        parents.append(intersect_layer)
    provenance = [Session.source_from_dataset(dataset)]
    if intersect_layer and intersect_layer in session.layers:
        provenance.extend(session.layers[intersect_layer].provenance)

    meta = LayerMeta(
        name=name,
        feature_count=int(n),
        geometry_type=dataset.geometry_type,
        bbox=bbox,
        attributes=schema,
        created_by="load",
        created_at=datetime.utcnow(),
        provenance=provenance,
        parent_layers=parents,
    )
    meta.attributes["__geom_col__"] = geom_col or ""
    session.register(meta)
    session.log(Operation(
        tool="load",
        args={
            "dataset_id": dataset.id,
            "bbox": list(bbox_3011) if bbox_3011 else None,
            "limit": limit,
            "where": where,
            "intersect_layer": intersect_layer,
        },
        result_layer=name,
        summary=f"{n} features"
        + (f" (truncated at {cap})" if truncated else ""),
        at=datetime.utcnow(),
    ))
    return meta


# Phase 2 operations (filter, spatial ops, stats, execute_sql, sources) live
# in geodata_mcp.operations to keep this file focused on catalog -> DuckDB loading.
