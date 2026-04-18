"""FastMCP server exposing the Phase 1 tools.

Run for stdio (testing with Claude Desktop / mcp CLI):
    uv run python -m geodata_mcp

Run as HTTP/SSE (deployment):
    uv run python -m geodata_mcp --http --port 8000

The HTTP mode also serves the viewer at /view/{session_id}.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from typing import Literal

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from .catalog import Catalog, DatasetEntry
from .geocoder import geocode as do_geocode
from .loader import LoadError, load_dataset
from .operations import (
    OpError, SqlError,
    EXPORT_ROOT, EXPORT_TTL_S,
    add_field as op_add_field,
    annotate as op_annotate,
    batch_iterate as op_batch_iterate,
    checkpoint as op_checkpoint,
    commit as op_commit,
    create_layer as op_create_layer,
    drop_field as op_drop_field,
    drop_layer as op_drop_layer,
    execute_sql as op_execute_sql,
    export_layer as op_export,
    filter_layer,
    inspect_location as op_inspect_location,
    list_layers as op_list_layers,
    rename_layer as op_rename_layer,
    rollback as op_rollback,
    set_notes as op_set_notes,
    sources as op_sources,
    spatial_buffer, spatial_centroid, spatial_clip, spatial_convex_hull,
    spatial_dissolve, spatial_intersect, spatial_select_by_location,
    stats as op_stats,
    update_field as op_update_field,
)
from .session import REGISTRY, Session, SessionExpired

ROOT = Path(__file__).resolve().parents[1]

# One catalog instance for the lifetime of the server.
CATALOG = Catalog.load()

SERVER_INSTRUCTIONS = """\
# Geodata MCP — Stockholm open geodata

This server exposes Swedish open geodata (SCB DeSO + Stockholm SBK Stadskarta)
to LLM clients via a session-scoped DuckDB spatial backend. Everything is
pre-filtered to Stockholm kommun (kommunkod `0180`) in EPSG:3011.

## Core workflow pattern

1. `search_data(query)` — fuzzy-find catalog datasets (SV + EN). 65 datasets total.
2. `load(dataset_id, ...)` or `load_many([ids])` — register as session layer(s).
3. Analyse:
   - `filter` / `spatial` / `stats` / `execute_sql` — derive new layers (immutable).
   - `add_field` / `update_field` / `drop_field` — mutate a layer's attribute table in place (QGIS Field-Calculator style).
   - `annotate(layer, {id: {...}})` — attach LLM-classified per-feature attributes in one call.
   - `batch_iterate(layer, ...)` — paginate large layers with a cursor to feed `annotate`.
   - `inspect_location(x, y, radius_m)` — "what's here?" across all layers in one call.
4. Cite with `sources(layer)` then `export(layer, format)` for a download URL.

## Reversible mutations via checkpoint / rollback

In-place mutations (`add_field`, `update_field`, `drop_field`, `annotate`,
`drop_layer`, `rename_layer`) are reversible if you wrap them in a checkpoint:

    checkpoint("before_enrichment")
    ... mutations ...
    rollback("before_enrichment")    # undo everything
    # or
    commit("before_enrichment")      # make it permanent, discard snapshots

Snapshots are column-scoped (O(changed columns × rows), not O(layer)). One
checkpoint active at a time; nested checkpoints not supported.

## Layer notes

Attach narrative with `set_notes(layer, "text")`. Surfaces in `list_layers` and
`sources`. Useful for recording *why* a layer exists ("filtered to pre-1940 stone
buildings as a proxy for the historical core").

## Coordinate reference system

- **All session layers are in EPSG:3011** (SWEREF 99 18 00; Stockholm-local metres).
- Bounding boxes, `x_3011`/`y_3011`, buffer distances: **metres in EPSG:3011**.
- Geocode results return EPSG:3011 coordinates.
- Exports to GeoJSON reproject to EPSG:4326; GPKG stays native EPSG:3011.

## Footguns to watch for

- **SCB privacy suppression**: small-population DeSOs have NULL values in
  statistical tables. Always `WHERE value IS NOT NULL` when computing numeric
  stats over SCB tables, or you'll get misleading averages.
- **DeSO 2018 → 2025 codes changed** in some areas; use `deso_historical_changes`
  or `deso_regso_mapping` to translate.
- **Attributes are Swedish**: `byggar` = year built, `antal` = count,
  `KATEGORI`/`GRUPP` = category/group. The catalog's per-attribute description
  field lists bilingual names and sample values — consult it, don't guess.
- **The geometry column is always `geom`** in every normalized layer. `ST_Read`
  returns it under that name — no need to probe or special-case.
- **`execute_sql` is read-only and sandboxed**: no INSERT/UPDATE/DELETE/DDL, no
  file readers, no HTTP URLs, no abs paths. Numeric literals > 10 M are
  rejected as DoS protection. For writes, use `add_field` / `update_field` /
  `annotate` instead.

## When the LLM is classifying features

Classic "I need to categorize every building by era" pattern:

    checkpoint("classify")
    batch_iterate("buildings", columns=["id","name","byggar"], batch_size=200)
    # → LLM reasons on the batch, returns {rowid: {"era": "functionalist", ...}, ...}
    annotate("buildings", values={...})
    # → repeat batch_iterate with cursor until exhausted
    commit("classify")

`annotate` creates columns on the fly if they don't exist.

## If something goes wrong

Every error response carries `error` + `detail`. Common codes:
- `unknown_dataset` — check `search_data()` first
- `sql_rejected` — validator blocked the SQL; see `detail` for which rule
- `sql_failed` — DuckDB execution error; check column names / types
- `op_failed` — generic op error; `detail` explains
- `session_expired` — idle > 30 min; see `replay` for operation log
"""


mcp = FastMCP("geodata-mcp", instructions=SERVER_INSTRUCTIONS)


# Common annotation shapes.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_SAFE_MUTATION = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=False, openWorldHint=False,
)
_IDEMPOTENT_MUTATION = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=True, openWorldHint=False,
)
_DESTRUCTIVE_MUTATION = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True,
    idempotentHint=False, openWorldHint=False,
)


def _checkpoint_hint(sess: Session) -> str | None:
    """Tip the LLM about checkpoint state after a reversible mutation."""
    if sess.active_checkpoint:
        return (
            f"Mutation is reversible — call rollback('{sess.active_checkpoint}') "
            f"to undo, commit('{sess.active_checkpoint}') to make permanent."
        )
    return (
        "No checkpoint active — this mutation is not reversible. "
        "Call checkpoint('name') first if you want undo capability."
    )


def _session(ctx: Context | None) -> Session:
    """Resolve the per-MCP-connection session. If ctx is None (stdio boot time)
    we fall back to a process-wide 'default' session. For streamable-HTTP each
    client request carries a session id that FastMCP threads through Context."""
    sid = getattr(ctx, "session_id", None) if ctx else None
    return REGISTRY.get_or_create(sid or "default")


def _error_response(e: Exception) -> dict:
    if isinstance(e, SessionExpired):
        return {"error": "session_expired", "detail": str(e),
                "replay": e.replay_info}
    if isinstance(e, SqlError):
        return {"error": "sql_rejected", "detail": str(e)}
    if isinstance(e, OpError) or isinstance(e, LoadError):
        return {"error": "op_failed", "detail": str(e)}
    return {"error": type(e).__name__, "detail": str(e)}


# ---------- helpers ----------

def _dataset_summary(d: DatasetEntry) -> dict:
    return {
        "id": d.id,
        "name": d.name_sv,
        "name_en": d.name_en,
        "description": d.description_sv,
        "description_en": d.description_en,
        "source_type": d.source_type,
        "geometry_type": d.geometry_type,
        "feature_count": d.feature_count,
        "coverage": d.coverage,
        "temporal": d.temporal,
        "crs_epsg": d.crs_epsg,
        "publisher": d.publisher,
        "license": d.license,
        "attributes": [
            {
                "name": a.name, "type": a.type,
                "description": a.description_sv,
                "description_en": a.description_en,
                "sample_values": a.sample_values,
            }
            for a in d.attributes
        ],
    }


def _layer_summary(sess: Session, name: str) -> dict:
    m = sess.layers[name]
    return {
        "name": m.name,
        "feature_count": m.feature_count,
        "geometry_type": m.geometry_type,
        "bbox_3011": list(m.bbox) if m.bbox else None,
        "attributes": {k: v for k, v in m.attributes.items() if not k.startswith("__")},
        "provenance": [
            {
                "dataset_id": s.dataset_id, "source_name": s.source_name,
                "publisher": s.publisher, "license": s.license,
                "url": s.url, "retrieved": s.retrieved,
                "llm_sourced": s.llm_sourced,
            }
            for s in m.provenance
        ],
    }


# ---------- tools ----------

@mcp.tool(annotations=_READ_ONLY)
def search_data(query: str = "", limit: int = 20) -> dict:
    """Fuzzy-search the catalog of locally available datasets.

    Args:
        query: Free-text search (Swedish or English). Empty string returns all datasets
               up to `limit`.
        limit: Max matches to return (default 20). Pass a larger value for exhaustive listing.

    Returns: structured metadata per matching dataset (name, description, coverage,
    temporal range, attribute schema with sample values, license, publisher).
    No suggestions or recommendations — facts only.
    """
    hits = CATALOG.search(query, limit=max(1, min(int(limit), 200)))
    return {
        "query": query,
        "total_in_catalog": len(CATALOG.all()),
        "results": [
            {**_dataset_summary(d), "match_score": round(score, 1)}
            for d, score in hits
        ],
    }


@mcp.tool(annotations=_READ_ONLY)
def geocode(
    name: str,
    limit: int = 5,
    all_kinds: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Look up a place name or street+number in Stockholm; returns EPSG:3011
    coordinates + bbox.

    Composite addresses ('Upplandsgatan 15') are spatially paired with nearest
    AdressText points. Stadsdel/Distrikt/Kvarter matches return polygon-backed
    bboxes. Everything else is place-name match against NamnText_point labels.

    By default, results are deduplicated across GRUPP — one row per named
    thing. Set `all_kinds=True` to see every label variant (e.g. both the
    building-label and the block-label for the same building).

    Args:
        name: Free-text query.
        limit: Max number of matches (default 5).
        all_kinds: If True, keep separate rows for different GRUPP values.

    Returns: list of matches with (name, kind, EPSG:3011 x/y, bbox, score).
    Empty list means no match within Stockholm coverage.
    """
    sess = _session(ctx)
    matches = do_geocode(sess.conn, name, limit=limit, all_kinds=all_kinds)
    return {
        "query": name,
        "coverage": "Stockholm kommun",
        "matches": [
            {
                "name": m.name, "kind": m.grupp, "subkind": m.kategori,
                "x_3011": m.x_3011, "y_3011": m.y_3011,
                "bbox_3011": list(m.bbox_3011),
                "score": round(m.score, 4),
            }
            for m in matches
        ],
    }


@mcp.tool(annotations=_SAFE_MUTATION)
def load(
    dataset_id: str,
    bbox_3011: list[float] | None = None,
    limit: int | None = None,
    layer_name: str | None = None,
    where: str | None = None,
    intersect_layer: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Load a catalog dataset into the session as a queryable layer.

    Filters (AND-combined, applied before the feature cap):
      bbox_3011:       [xmin, ymin, xmax, ymax] in EPSG:3011
      where:           SQL WHERE clause on attributes (no semicolons).
                       Example: "KATEGORI = 'Stadsdel' AND NAMN = 'SÖDERMALM'"
      intersect_layer: Name of an already-loaded layer whose geometry defines
                       the spatial restriction. Preferred over hand-crafted
                       bboxes for irregular polygons (e.g. clip to a Stadsdel).

    Server-enforced cap: 100,000 features per load.

    Returns layer summary: name, feature_count, bbox, attribute schema, provenance.
    """
    entry = CATALOG.get(dataset_id)
    if entry is None:
        return {"error": "unknown_dataset", "dataset_id": dataset_id,
                "hint": "Call search_data() to list available datasets."}
    bbox_tuple = tuple(bbox_3011) if bbox_3011 and len(bbox_3011) == 4 else None
    try:
        sess = _session(ctx)
        meta = load_dataset(
            sess, entry,
            bbox_3011=bbox_tuple, limit=limit, layer_name=layer_name,
            where=where, intersect_layer=intersect_layer,
        )
    except Exception as e:
        return _error_response(e)
    return _layer_summary(sess, meta.name)


@mcp.tool(annotations=_SAFE_MUTATION)
def filter(
    layer: str, where: str,
    result_name: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Apply a SQL WHERE clause to an existing session layer; emit a new layer.

    The WHERE expression accepts **any DuckDB-compatible predicate**, including
    spatial predicates on the `geom` column:

      - Attribute: `KATEGORI = 'Flerbostadshus' AND NAMN IS NOT NULL`
      - Spatial point-in-polygon: `ST_Contains(geom, ST_Point(153700, 6578000))`
      - Spatial distance:         `ST_DWithin(geom, ST_Point(x, y), 200)`
      - Mixed:  `KATEGORI='Flerbostadshus' AND ST_Contains(geom, <polygon>)`

    For "features of A that relate to any feature of B" use
    `spatial(operation='select_by_location', ...)` instead — that handles
    cross-layer predicates directly.

    Args:
        layer: Source layer name (from a previous load/filter/spatial result).
        where: SQL WHERE expression (no ';'). DuckDB spatial functions available.
        result_name: Optional name for the new layer; defaults to "<layer>_filtered".

    Provenance of the source layer is inherited.
    """
    try:
        sess = _session(ctx)
        meta = filter_layer(sess, layer, where, result_name=result_name)
    except Exception as e:
        return _error_response(e)
    return _layer_summary(sess, meta.name)


SpatialOp = Literal[
    "clip", "intersect", "select_by_location",
    "buffer", "centroid", "dissolve", "convex_hull",
]
SpatialPredicate = Literal["intersects", "within", "contains", "dwithin"]


@mcp.tool(annotations=_SAFE_MUTATION)
def spatial(
    operation: SpatialOp,
    layer: str | None = None,
    by_layer: str | None = None,
    a_layer: str | None = None,
    b_layer: str | None = None,
    distance_m: float | None = None,
    by_columns: list[str] | None = None,
    aggregate: bool = False,
    predicate: SpatialPredicate = "intersects",
    result_name: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Spatial operation producing a new layer.

    Supported operations:

      - **select_by_location** (layer, by_layer, predicate) — features of
        `layer` kept unchanged (same geometry + same attributes) where their
        geom relates to ANY feature in `by_layer` by `predicate`. This is
        the classic "buildings in this district" / "DeSO containing this
        point" operation — use this when you want a spatial WHERE.
        Predicates: 'intersects' (default), 'within', 'contains', 'dwithin'.
        'dwithin' requires `distance_m` (metres, EPSG:3011).

      - **clip** (layer, by_layer) — trim `layer`'s geometries to the union
        of `by_layer`'s geometries. Geometries are MODIFIED.

      - **intersect** (a_layer, b_layer) — GEOMETRIC intersection overlay:
        one row per intersecting pair, geometry = `ST_Intersection(a, b)`
        (often changes geometry kind). For a spatial join that keeps A's
        geometry, use `select_by_location` instead.

      - **buffer** (layer, distance_m) — `ST_Buffer` in EPSG:3011 metres.

      - **centroid** (layer) — per-feature `ST_Centroid`.

      - **dissolve** (layer, by_columns?) — union geometries; optionally
        grouped by attribute columns. Adds a `feature_count` column.

      - **convex_hull** (layer, aggregate?) — per-feature hull, or a single
        aggregate hull of all geometries with `aggregate=true`.

    Args:
        operation: one of the op names above.
        layer / by_layer: used by clip, select_by_location, buffer,
                          centroid, dissolve, convex_hull.
        a_layer / b_layer: used by intersect (overlay).
        distance_m: required by buffer; also by select_by_location when
                    predicate='dwithin'.
        by_columns: optional group-by for dissolve.
        aggregate: flip convex_hull to aggregate mode.
        predicate: for select_by_location.
        result_name: optional name for the resulting layer.
    """
    try:
        sess = _session(ctx)
        if operation == "clip":
            if not layer or not by_layer:
                return {"error": "missing_arg", "detail": "clip requires `layer` and `by_layer`."}
            meta = spatial_clip(sess, layer, by_layer, result_name=result_name)
        elif operation == "select_by_location":
            if not layer or not by_layer:
                return {"error": "missing_arg", "detail": "select_by_location requires `layer` and `by_layer`."}
            meta = spatial_select_by_location(
                sess, layer, by_layer,
                predicate=predicate,
                distance_m=distance_m,
                result_name=result_name,
            )
        elif operation == "intersect":
            if not a_layer or not b_layer:
                return {"error": "missing_arg", "detail": "intersect requires `a_layer` and `b_layer`."}
            meta = spatial_intersect(sess, a_layer, b_layer, result_name=result_name)
        elif operation == "buffer":
            if not layer or distance_m is None:
                return {"error": "missing_arg", "detail": "buffer requires `layer` and `distance_m`."}
            meta = spatial_buffer(sess, layer, float(distance_m), result_name=result_name)
        elif operation == "centroid":
            if not layer:
                return {"error": "missing_arg", "detail": "centroid requires `layer`."}
            meta = spatial_centroid(sess, layer, result_name=result_name)
        elif operation == "dissolve":
            if not layer:
                return {"error": "missing_arg", "detail": "dissolve requires `layer`."}
            meta = spatial_dissolve(sess, layer, by_columns=by_columns, result_name=result_name)
        elif operation == "convex_hull":
            if not layer:
                return {"error": "missing_arg", "detail": "convex_hull requires `layer`."}
            meta = spatial_convex_hull(sess, layer, aggregate=aggregate, result_name=result_name)
        else:
            return {"error": "unsupported_operation", "operation": operation,
                    "supported": list(SpatialOp.__args__)}
    except Exception as e:
        return _error_response(e)
    return _layer_summary(sess, meta.name)


@mcp.tool(annotations=_READ_ONLY)
def stats(
    layer: str,
    columns: list[str] | None = None,
    group_by: list[str] | None = None,
    limit: int = 100,
    ctx: Context | None = None,
) -> str:
    """Summarize a layer as a markdown table.

    For each numeric column in `columns` returns count / min / avg / max. For
    string columns returns count / distinct_count. With `group_by`, produces
    one row per distinct combination of those columns (ordered by count DESC).

    Args:
        layer: Source layer.
        columns: Columns to summarize. If None, all numeric columns are used.
        group_by: Optional list of grouping columns.
        limit: Row cap (default 100).
    """
    try:
        return op_stats(_session(ctx), layer, columns=columns, group_by=group_by, limit=limit)
    except Exception as e:
        return f"error: {e}"


@mcp.tool(annotations=_SAFE_MUTATION)
def execute_sql(
    sql: str,
    description: str = "",
    result_name: str | None = None,
    geometry_column: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Run a validated, read-only SQL query against session layers.

    Only a single SELECT/WITH/UNION is accepted — DDL/DML is rejected by a
    sqlglot-based parser. DuckDB spatial functions are available. Session
    layers are referenced by their names as regular tables.

    Layer-vs-table decision:
      1. If `geometry_column` is passed, layer mode is forced with that column
         cast to GEOMETRY.
      2. Else the tool runs `DESCRIBE (sql)` and promotes to layer if any
         column has DuckDB type starting with `GEOMETRY`.
      3. Otherwise it returns up to 50 rows as a markdown table. If a column
         named `geom`/`geometry` exists but got demoted to BLOB (common with
         cross-layer expressions), the response carries a `geometry_hint`
         telling you to retry with `geometry_column='…'`.

    30-second wall-clock timeout, 256 MB per-session memory limit.

    Args:
        sql: Read-only SQL. No semicolons, no DDL, no multi-statement.
        description: Free-text label stored in the operation log.
        result_name: Optional name if the query yields a layer.
        geometry_column: Explicit geometry-column hint; forces layer mode.
    """
    try:
        return op_execute_sql(
            _session(ctx), sql,
            description=description,
            result_name=result_name,
            geometry_column=geometry_column,
        )
    except SqlError as e:
        return {"error": "sql_rejected", "detail": str(e)}
    except SessionExpired as e:
        return _error_response(e)
    except OpError as e:
        return {"error": "sql_failed", "detail": str(e)}


@mcp.tool(annotations=_SAFE_MUTATION)
def create_layer(
    name: str,
    data: list[dict],
    source: str | None = None,
    geometry_column: str | None = None,
    crs: str = "EPSG:4326",
    ctx: Context | None = None,
) -> dict:
    """Inject LLM-provided data as a new session layer.

    Use this when the LLM brings data that isn't in the catalog — e.g. a
    manually curated lookup table, a transcription of external research, a
    simulated result — to join with catalog layers. The data is materialized
    in the session's DuckDB instance so all other tools (filter, spatial,
    stats, execute_sql, sources) can use it.

    Args:
        name: Desired layer name. Collisions are suffixed (`_2`, `_3`, …).
        data: Up to 1,000 rows. List of dicts; each dict is one row with
              identical keys. Values may be any JSON-serializable scalar.
        source: Short free-text description of where you got this data
                (e.g. "Booli.se 2026-03 scrape", "manual count of tram stops
                from SL.se timetable"). Stored in provenance as
                `llm_source_description` and always marked `llm_sourced=True`.
        geometry_column: If one of the columns contains WKT strings (e.g.
                         "POINT(18.07 59.33)" or "POLYGON((…))"), name it here.
                         It'll be parsed and reprojected to EPSG:3011.
        crs: EPSG code of the input geometry. Default 'EPSG:4326' (lng/lat).

    Returns the new layer's summary.
    """
    try:
        sess = _session(ctx)
        meta = op_create_layer(
            sess, name, data,
            source=source, geometry_column=geometry_column, crs=crs,
        )
    except Exception as e:
        return _error_response(e)
    return _layer_summary(sess, meta.name)


@mcp.tool(annotations=_SAFE_MUTATION)
def export(
    layer: str,
    format: str = "geojson",
    ctx: Context | None = None,
) -> dict:
    """Export a session layer to a downloadable file. Returns a URL valid for 24 h.

    Supported formats:
      - **geojson**: EPSG:4326 FeatureCollection (portable)
      - **gpkg**: OGC GeoPackage in native EPSG:3011
      - **csv**: attribute columns + geometry as WKT
      - **parquet**: columnar, zstd-compressed, geometry as WKB

    The returned URL lives under /exports/<random-token>/<filename> on the same
    host as the MCP endpoint. Links auto-expire after 24 h.
    """
    try:
        return op_export(_session(ctx), layer, fmt=format)
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_READ_ONLY)
def sources(layer: str | None = None, ctx: Context | None = None) -> str:
    """Return a structured provenance report (publisher, license, URL,
    retrieval date, operations applied) for a layer or all session layers.

    Use this to cite where data came from after a multi-step analysis.
    """
    return op_sources(_session(ctx), layer)


@mcp.tool(annotations=_READ_ONLY)
def inspect(
    layer: str,
    n: int = 10,
    include_geometry: bool = False,
    offset: int = 0,
    where: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Show raw rows from a session layer as a markdown table.

    Hard caps: 200 rows without geometry, 10 rows with geometry (WKT). Geometry
    is verbose — only request it when you actually need to see coordinates. For
    inspecting very large layers column-wise, use `batch_iterate` instead — it
    returns a resumable cursor.

    Args:
        layer: Layer name from a previous load/filter/spatial result.
        n: Rows to return (default 10; capped at 200 without geometry, 10 with).
        include_geometry: If True, append a 'geom_wkt' column.
        offset: Row offset for pagination.
        where: Optional SQL WHERE expression (no semicolons).
    """
    try:
        sess = _session(ctx)
    except SessionExpired as e:
        return f"error: {e}"
    meta = sess.layers.get(layer)
    if meta is None:
        return f"error: unknown layer '{layer}'. Available: {list(sess.layers)}"

    cap = 10 if include_geometry else 200
    n = max(1, min(int(n), cap))

    geom_col = meta.attributes.get("__geom_col__") or ""
    cols = [c for c in meta.attributes if not c.startswith("__")]
    select_cols = []
    for c in cols:
        if c == geom_col:
            if include_geometry:
                select_cols.append(f'ST_AsText({_qi(c)}) AS geom_wkt')
            # else skip — geometry omitted
        else:
            select_cols.append(_qi(c))

    where_sql = ""
    if where:
        if ";" in where:
            return "error: WHERE expression cannot contain ';'"
        where_sql = f" WHERE {where}"
    sql = (
        f"SELECT {', '.join(select_cols)} FROM {_qi(layer)}"
        f"{where_sql} LIMIT {n} OFFSET {int(offset)}"
    )
    try:
        rows = sess.conn.execute(sql).fetchall()
        col_names = [d[0] for d in sess.conn.description]
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"

    if not rows:
        return f"(no rows; {meta.feature_count} in layer)"
    md = ["| " + " | ".join(col_names) + " |",
          "|" + "|".join(["---"] * len(col_names)) + "|"]
    for r in rows:
        md.append("| " + " | ".join(_md_cell(v) for v in r) + " |")
    suffix = ""
    if include_geometry:
        suffix = "\n\n_geom_wkt is large — request only when needed._"
    return f"Showing {len(rows)} of {meta.feature_count} rows in `{layer}`.\n\n" + "\n".join(md) + suffix


@mcp.tool(annotations=_IDEMPOTENT_MUTATION)
def show(
    layers: list[str],
    title: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Mark layers visible in the viewer and return their summaries + viewer URL.

    The text response is fully usable on its own — opening the viewer is optional.
    """
    try:
        sess = _session(ctx)
    except SessionExpired as e:
        return _error_response(e)
    summaries = []
    missing = []
    for n in layers:
        if n in sess.layers:
            summaries.append(_layer_summary(sess, n))
        else:
            missing.append(n)
    sess.visible_layers = [n for n in layers if n in sess.layers]
    return {
        "title": title,
        "viewer_url": f"/view/{sess.id}",
        "visible_layers": summaries,
        "unknown_layers": missing,
    }


# ---------- P1 tool wrappers ----------


@mcp.tool(annotations=_READ_ONLY)
def list_layers(ctx: Context | None = None) -> dict:
    """Inventory of every layer in the current session.

    Returns: `n_layers`, per-layer `{name, feature_count, geometry_type,
    bbox_3011, columns, created_by, parent_layers, notes, is_visible}`,
    plus `active_checkpoint` and `open_checkpoints`. Call this when the LLM
    needs to recall what it has or when it looks overwhelmed by prior state.
    """
    try:
        return op_list_layers(_session(ctx))
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_SAFE_MUTATION)
def load_many(
    dataset_ids: list[str],
    bbox_3011: list[float] | None = None,
    limit: int | None = None,
    ctx: Context | None = None,
) -> dict:
    """Bulk-load several catalog datasets in one call. Identical semantics to
    `load` but applied across a list. The same bbox/limit apply to every
    dataset in the list — use single `load` calls if you need per-dataset
    arguments.

    Returns: a list of summaries, plus a list of `errors` (per-dataset).
    """
    results = []
    errors = []
    bbox_tuple = tuple(bbox_3011) if bbox_3011 and len(bbox_3011) == 4 else None
    try:
        sess = _session(ctx)
    except SessionExpired as e:
        return _error_response(e)
    for did in dataset_ids:
        entry = CATALOG.get(did)
        if entry is None:
            errors.append({"dataset_id": did, "error": "unknown_dataset"})
            continue
        try:
            meta = load_dataset(sess, entry, bbox_3011=bbox_tuple, limit=limit)
            results.append(_layer_summary(sess, meta.name))
        except Exception as e:
            errors.append({"dataset_id": did, "error": type(e).__name__, "detail": str(e)})
    return {"loaded": results, "errors": errors, "n_loaded": len(results)}


@mcp.tool(annotations=_SAFE_MUTATION)
def add_field(
    layer: str, name: str, expr: str,
    field_type: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Add a new column to a layer in place, computed from a SQL expression.

    QGIS / ArcGIS Field Calculator pattern. The expression is evaluated per row
    and may reference other columns of the same layer or scalar subqueries
    against other session layers. Type is inferred unless `field_type` is set
    (VARCHAR / DOUBLE / BIGINT / BOOLEAN / DATE / TIMESTAMP).

    Reversible when inside an active `checkpoint(...)`. Use for:
      - era classifications: `CASE WHEN byggar < 1900 THEN 'pre-modern' END`
      - area/density: `ST_Area(geom)`, `population / ST_Area(geom)`
      - joins as columns: `(SELECT val FROM my_lookup WHERE id = layer.id)`

    Args:
        layer: target layer.
        name: new column name (must not already exist — see `update_field`).
        expr: DuckDB SQL scalar expression.
        field_type: optional type override. If None, inferred.
    """
    try:
        sess = _session(ctx)
        out = op_add_field(sess, layer, name, expr, field_type=field_type)
        out["hint"] = _checkpoint_hint(sess)
        return out
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_SAFE_MUTATION)
def update_field(
    layer: str, name: str, expr: str,
    where: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Overwrite an existing column's values from a SQL expression, optionally
    restricted by WHERE. In place. Reversible inside a checkpoint.

    Args:
        layer: target layer.
        name: column to overwrite.
        expr: DuckDB SQL scalar expression.
        where: optional WHERE clause restricting which rows are updated.
    """
    try:
        sess = _session(ctx)
        out = op_update_field(sess, layer, name, expr, where=where)
        out["hint"] = _checkpoint_hint(sess)
        return out
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_SAFE_MUTATION)
def drop_field(layer: str, name: str, ctx: Context | None = None) -> dict:
    """Remove a column from a layer. In place. Reversible inside a checkpoint.
    Refuses to drop the geometry column — use drop_layer for that."""
    try:
        sess = _session(ctx)
        out = op_drop_field(sess, layer, name)
        out["hint"] = _checkpoint_hint(sess)
        return out
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_SAFE_MUTATION)
def annotate(
    layer: str,
    values: dict,
    key_column: str = "rowid",
    ctx: Context | None = None,
) -> dict:
    """Attach LLM-classified per-feature attributes in one call.

    Payload shape:
        values = {
            "<key>": {"era": "functionalist", "confidence": 0.9, "note": "..."},
            "<key>": {"era": "art-nouveau",    "confidence": 0.7, ...},
            ...
        }

    Columns are created on the fly if they don't exist (type inferred from the
    values: all-int → BIGINT, int/float mix → DOUBLE, bool → BOOLEAN, else
    VARCHAR). Up to 10,000 keys per call. Pair with `batch_iterate` for layers
    larger than you can reason about in one pass.

    Args:
        layer: target layer.
        values: {key_value: {attr_name: val, ...}} — many features per call.
        key_column: column to match on. Default 'rowid' (DuckDB pseudo-column,
                    stable within a session). Use a declared key column when
                    one exists.

    Reversible inside a checkpoint (pre-image snapshotted once per column).
    """
    try:
        sess = _session(ctx)
        out = op_annotate(sess, layer, values, key_column=key_column)
        out["hint"] = _checkpoint_hint(sess)
        return out
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_READ_ONLY)
def batch_iterate(
    layer: str | None = None,
    columns: list[str] | None = None,
    batch_size: int = 200,
    cursor: str | None = None,
    where: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Paginate through a layer with a resumable cursor. Use for layers too
    large to fit in a single inspect/annotate call.

    First call: pass `layer` (and optionally `columns`, `where`, `batch_size`).
    The response carries `rows`, `next_cursor`, and `exhausted`.
    Subsequent calls: pass `cursor=<next_cursor>` — all other args ignored.
    Finish when `next_cursor` is None.

    Every batch includes a `rowid` column (DuckDB pseudo-column) suitable for
    `annotate(..., key_column='rowid')`.

    Args:
        layer: source layer name (first call only).
        columns: column subset (first call only). Defaults to all non-geometry.
        batch_size: 1 to 500 rows per batch (default 200).
        cursor: opaque token from a previous batch.
        where: SQL WHERE to restrict the iteration (first call only).
    """
    try:
        sess = _session(ctx)
        if cursor is None and not layer:
            return {"error": "missing_arg",
                    "detail": "first call requires `layer`; subsequent calls use `cursor`."}
        return op_batch_iterate(
            sess, layer or "",  # layer is required for the first call
            columns=columns, batch_size=batch_size,
            cursor=cursor, where=where,
        )
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_READ_ONLY)
def inspect_location(
    x_3011: float,
    y_3011: float,
    radius_m: float = 100.0,
    layers: list[str] | None = None,
    per_layer_limit: int = 5,
    ctx: Context | None = None,
) -> dict:
    """What's here? One-shot spatial lookup near a point across many layers.

    Returns, for each session layer with geometry (or the specified subset),
    up to `per_layer_limit` features whose geometry is within `radius_m` of
    (x_3011, y_3011), sorted by distance. Each feature carries its attributes
    plus a `distance_m` float.

    Args:
        x_3011, y_3011: query point in EPSG:3011.
        radius_m: search radius in metres (default 100).
        layers: optional subset of layer names; defaults to all with geometry.
        per_layer_limit: 1..25, default 5.
    """
    try:
        sess = _session(ctx)
        return op_inspect_location(
            sess, x_3011, y_3011,
            radius_m=radius_m, layers=layers,
            per_layer_limit=per_layer_limit,
        )
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_SAFE_MUTATION)
def drop_layer(name: str, ctx: Context | None = None) -> dict:
    """Remove a layer from the session. Reversible inside an active checkpoint
    (full layer snapshotted); not reversible otherwise.
    """
    try:
        sess = _session(ctx)
        out = op_drop_layer(sess, name)
        out["hint"] = _checkpoint_hint(sess)
        return out
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_SAFE_MUTATION)
def rename_layer(old: str, new: str, ctx: Context | None = None) -> dict:
    """Rename a layer. Reversible inside an active checkpoint."""
    try:
        sess = _session(ctx)
        out = op_rename_layer(sess, old, new)
        out["hint"] = _checkpoint_hint(sess)
        return out
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_IDEMPOTENT_MUTATION)
def set_notes(layer: str, notes: str, ctx: Context | None = None) -> dict:
    """Attach free-text notes to a layer. Shown in list_layers and sources.
    Useful for "why does this layer exist" narration that will help you (or a
    colleague reading the export) later."""
    try:
        return op_set_notes(_session(ctx), layer, notes)
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_SAFE_MUTATION)
def checkpoint(name: str, ctx: Context | None = None) -> dict:
    """Create a named checkpoint. Subsequent in-place mutations (`add_field`,
    `update_field`, `drop_field`, `annotate`, `drop_layer`, `rename_layer`)
    snapshot their pre-image; call `rollback(name)` to undo all mutations, or
    `commit(name)` to make them permanent and discard snapshots.

    One checkpoint may be active at a time. Snapshots are column-scoped,
    so storage cost scales with the *diff*, not the full layer.
    """
    try:
        return op_checkpoint(_session(ctx), name)
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_SAFE_MUTATION)
def rollback(name: str, ctx: Context | None = None) -> dict:
    """Restore every in-place mutation made since `checkpoint(name)`. Discards
    the checkpoint and its snapshots."""
    try:
        return op_rollback(_session(ctx), name)
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_SAFE_MUTATION)
def commit(name: str, ctx: Context | None = None) -> dict:
    """Make all mutations since `checkpoint(name)` permanent. Discards
    snapshots, reclaims storage."""
    try:
        return op_commit(_session(ctx), name)
    except Exception as e:
        return _error_response(e)


# ---------- helpers ----------

def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _md_cell(v) -> str:
    if v is None:
        return ""
    s = str(v).replace("\n", " ").replace("|", "\\|")
    return s if len(s) < 80 else s[:77] + "..."


# ---------- HTTP / viewer routes (added when run with --http) ----------

class RateLimitMiddleware:
    """Token-bucket rate limiter per client IP. Applies to all routes except
    /static/*. Generous defaults — this is belt-and-braces, not a production DDoS guard."""

    def __init__(self, app, rate_per_min: int = 120, burst: int = 40) -> None:
        self.app = app
        self.rate = rate_per_min / 60.0
        self.burst = burst
        self._buckets: dict[str, tuple[float, float]] = {}  # ip → (tokens, last_ts)
        import threading
        self._lock = threading.Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path.startswith("/static"):
            return await self.app(scope, receive, send)
        # Client IP — Cloudflare fronts us, so prefer CF-Connecting-IP.
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        ip = headers.get("cf-connecting-ip") or headers.get("x-forwarded-for", "").split(",")[0].strip()
        if not ip:
            ip = scope.get("client", ["unknown"])[0] or "unknown"
        import time as _time
        now = _time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(ip, (self.burst, now))
            tokens = min(self.burst, tokens + self.rate * (now - last))
            if tokens < 1:
                self._buckets[ip] = (tokens, now)
                retry_after = int((1 - tokens) / self.rate) + 1
                from starlette.responses import JSONResponse as _JR
                response = _JR(
                    {"error": "rate_limited", "retry_after_s": retry_after},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
                return await response(scope, receive, send)
            tokens -= 1
            self._buckets[ip] = (tokens, now)
        return await self.app(scope, receive, send)


def build_http_app() -> object:
    """Compose FastMCP's streamable-HTTP routes with the viewer/API routes."""
    import hashlib
    from starlette.applications import Starlette
    from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    viewer_dir = ROOT / "viewer"
    # Content-hash the JS at startup so the viewer HTML references /static/app.js?v=<hash>.
    # Changes to the JS auto-bust Cloudflare's cache-control: max-age=14400.
    app_js_hash = hashlib.sha256((viewer_dir / "app.js").read_bytes()).hexdigest()[:10]
    index_template = (viewer_dir / "index.html").read_text(encoding="utf-8")
    index_rendered = index_template.replace("{APP_JS_HASH}", app_js_hash)

    async def view_index(request):
        return HTMLResponse(index_rendered)

    async def api_visible(request):
        sid = request.path_params["session_id"]
        s = REGISTRY.get(sid)
        if s is None:
            return JSONResponse(
                {"error": "unknown_or_expired_session", "session_id": sid},
                status_code=404,
            )
        return JSONResponse({
            "session_id": s.id,
            "visible_layers": s.visible_layers,
            "layers": {n: _layer_summary(s, n) for n in s.visible_layers},
        })

    async def serve_export(request):
        """Serve /exports/{token}/{filename} from data/exports/.
        Path-traversal-safe: token must be a pure identifier, filename must live
        inside <EXPORT_ROOT>/<token>/."""
        token = request.path_params["token"]
        filename = request.path_params["filename"]
        if not token.replace("-", "").replace("_", "").isalnum():
            return JSONResponse({"error": "bad_token"}, status_code=400)
        if "/" in filename or ".." in filename:
            return JSONResponse({"error": "bad_filename"}, status_code=400)
        path = (EXPORT_ROOT / token / filename).resolve()
        if not str(path).startswith(str(EXPORT_ROOT.resolve())):
            return JSONResponse({"error": "bad_path"}, status_code=400)
        if not path.exists():
            return JSONResponse({"error": "not_found"}, status_code=404)
        # Check expiry — anything older than EXPORT_TTL is refused and cleaned up.
        import time as _time
        if _time.time() - path.stat().st_mtime > EXPORT_TTL_S:
            try:
                path.unlink()
                path.parent.rmdir()
            except OSError:
                pass
            return JSONResponse({"error": "expired"}, status_code=410)
        return FileResponse(path, filename=filename)

    async def api_layer_geojson(request):
        sid = request.path_params["session_id"]
        layer = request.path_params["layer"]
        s = REGISTRY.get(sid)
        if s is None or layer not in s.layers:
            return JSONResponse({"error": "unknown_layer_or_session"}, status_code=404)
        meta = s.layers[layer]
        geom_col = meta.attributes.get("__geom_col__") or ""
        if not geom_col:
            return JSONResponse({"type": "FeatureCollection", "features": []})
        # Reproject EPSG:3011 → EPSG:4326 at the API boundary, emit GeoJSON.
        cols = [c for c in meta.attributes if not c.startswith("__") and c != geom_col]
        props_struct = ", ".join(f"'{c}', {_qi(c)}" for c in cols) or "'_', NULL"
        sql = f"""
            SELECT json_object(
                'type', 'FeatureCollection',
                'features', json_group_array(json_object(
                    'type', 'Feature',
                    'properties', json_object({props_struct}),
                    'geometry', ST_AsGeoJSON(ST_Transform({_qi(geom_col)}, 'EPSG:3011', 'EPSG:4326', true))::JSON
                ))
            ) FROM {_qi(layer)}
        """
        try:
            (payload,) = s.conn.execute(sql).fetchone()
            return Response(payload, media_type="application/json")
        except Exception as e:
            return JSONResponse(
                {"error": "geojson_failed", "detail": f"{type(e).__name__}: {e}"},
                status_code=500,
            )

    # FastMCP's http_app provides a /mcp route AND a lifespan that starts the
    # streamable-http session manager. We must (a) include its routes directly
    # (Mount double-prefixes the path) and (b) propagate its lifespan.
    mcp_app = mcp.http_app(transport="http")

    routes = [
        *mcp_app.routes,
        Route("/view/{session_id}", view_index),
        Route("/api/{session_id}/visible_layers", api_visible),
        Route("/api/{session_id}/layer/{layer}/geojson", api_layer_geojson),
        Route("/exports/{token}/{filename}", serve_export),
        Mount("/static", StaticFiles(directory=str(viewer_dir)), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=mcp_app.router.lifespan_context)
    # Wrap with a simple per-IP token bucket (120 req/min, burst 40).
    return RateLimitMiddleware(app)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="Serve over HTTP/SSE")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    # Idle sessions GC to disk + structured SessionExpired on next touch.
    REGISTRY.start_gc()

    if args.http:
        import uvicorn
        app = build_http_app()
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        # stdio for direct MCP-client use
        mcp.run()


if __name__ == "__main__":
    main()
