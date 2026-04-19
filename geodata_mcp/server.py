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
    hide_layers as op_hide_layers,
    inspect_location as op_inspect_location,
    inspect_locations as op_inspect_locations,
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

# Public URL prefix for viewer / export links. Empty → relative paths (dev).
# Production: set GEODATA_PUBLIC_URL=https://geo.benjaminhenriksson.com.
import os as _os
PUBLIC_URL = _os.environ.get("GEODATA_PUBLIC_URL", "").rstrip("/")


def _abs_url(path: str) -> str:
    """Prepend PUBLIC_URL if set, else return as-is (relative)."""
    if PUBLIC_URL and path.startswith("/"):
        return f"{PUBLIC_URL}{path}"
    return path


# One catalog instance for the lifetime of the server.
CATALOG = Catalog.load()

SERVER_INSTRUCTIONS = """\
# Geodata MCP — Stockholm open geodata

This server exposes Swedish open geodata (SCB DeSO + Stockholm SBK Stadskarta)
to LLM clients via a session-scoped DuckDB spatial backend. Everything is
pre-filtered to Stockholm kommun (kommunkod `0180`) in EPSG:3011.

## Core workflow pattern

1. `search_data(query, verbose=False)` — fuzzy-find catalog datasets (SV+EN).
   65 datasets total. `verbose=False` (default) returns compact summaries; call
   `describe_dataset(id)` for the full attribute schema once you know what you want.
2. **Prefer `load_many([ids])` over repeated `load(id)`** when you need more
   than one dataset — one round-trip vs. N. Fall back to single `load` only
   when per-dataset bbox/where are needed.
3. Analyse:
   - `filter` / `spatial` / `stats` / `execute_sql` — derive new layers (immutable).
   - `add_field` / `update_field` / `drop_field` — mutate a layer's attribute
     table in place (QGIS Field-Calculator style).
   - `annotate(layer, {id: {...}})` — attach LLM-classified per-feature
     attributes in one call (up to 10,000 keys).
   - `batch_iterate(layer, ...)` — paginate large layers with a cursor to feed
     `annotate`.
   - `inspect_location(x, y, radius_m)` — "what's here?" one point, all layers.
   - `inspect_locations(points, radius_m)` — same but for many points at once.
4. Visualize: `show(layers)` + open the returned viewer URL.
5. Cite with `sources(layer)` then `export(layer, format)` for a download URL.

## Reversible mutations via checkpoint / rollback

In-place mutations (`add_field`, `update_field`, `drop_field`, `annotate`,
`drop_layer`, `rename_layer`) are reversible if you wrap them in a checkpoint:

    checkpoint("before_enrichment")                     # snapshots whatever mutates
    # ... mutations ...
    rollback("before_enrichment")                        # undo everything
    # or
    commit("before_enrichment")                          # make permanent

**Scoped checkpoints** — pass `layers=[...]` to restrict the checkpoint to
specific layers:

    checkpoint("era_work", layers=["buildings"])         # only snapshots `buildings`
    add_field("buildings", "era", "...")                 # covered
    add_field("roads", "surface", "...")                 # NOT in this checkpoint's scope

Multiple checkpoints can be active simultaneously (on non-overlapping or
overlapping scopes — a mutation snapshots for every active checkpoint that
covers that layer). Snapshots are column-scoped (O(changed columns × rows)).

## Bulk enrichment pattern (the canonical AI-native loop)

    load("sbk_buildings")
    checkpoint("classify", layers=["sbk_buildings"])
    out = batch_iterate("sbk_buildings", columns=["id","name","byggar"], batch_size=500)
    while True:
        tags = {rowid: {"era": ..., "confidence": ...} for rowid in out.rows}
        annotate("sbk_buildings", values=tags)
        if out.exhausted: break
        out = batch_iterate(cursor=out.next_cursor)    # batch_size honored per call
    commit("classify")

`annotate` creates columns on the fly. For >10k features, drive it with
`create_layer(payload)` as a side-table + `add_field` subquery join instead of
inline JSON.

## Layer notes

Attach narrative with `set_notes(layer, "text")`. Surfaces in `list_layers`
and `sources`. Useful for recording *why* a layer exists ("filtered to
pre-1940 stone buildings as a proxy for the historical core") so the
reasoning is recoverable from session state alone.

## Injecting LLM-found data — `source` is mandatory

Every row you inject via `create_layer` must carry a `source` attribute so
provenance survives filtering, joining, and export. Two ways to provide it:

  - Top-level `source="Booli.se 2026-03 scrape"` — broadcast to every row.
    Use this when all rows share one origin.
  - Per-row `source` field in each dict — use when rows come from different
    origins (some from hitta.se, some from web search, some from the user).

Failing to provide either form returns `missing_arg`. Short, specific
source strings are the norm ("SL.se timetable 2026-04", "hitta.se manual
lookup 2026-04-19"), not generic ones like "the internet". When the LLM
has mixed sources, prefer the per-row form so the user can later filter
by `source` to audit what came from where.

## Coordinate reference system

- **All session layers are in EPSG:3011** (SWEREF 99 18 00; Stockholm-local metres).
- Bounding boxes, `x_3011`/`y_3011`, buffer distances: **metres in EPSG:3011**.
- Geocode results return EPSG:3011 coordinates.
- Exports to GeoJSON reproject to EPSG:4326; GPKG stays native EPSG:3011.

## Footguns to watch for

- **SCB privacy suppression**: small-population DeSOs have NULL values in
  statistical tables. Always `WHERE value IS NOT NULL` when computing numeric
  stats, or you'll get misleading averages.
- **SCB region column**: every normalized SCB parquet carries `region`,
  `region_kind` ∈ {`deso`, `regso`, `kommun`, `country`}, `region_code`, and
  `region_name` columns. Filter by `region_kind = 'deso'` before joining to
  the DeSO polygon layer — don't use fragile `LIKE` hacks.
- **DeSO 2018 → 2025 codes changed** in some areas; use
  `deso_historical_changes` or `deso_regso_mapping` to translate.
- **Attributes are Swedish**: `byggar` = year built, `antal` = count,
  `KATEGORI`/`GRUPP` = category/group. Every SCB parquet's value column is
  now called `value` (unified at normalize time) regardless of the SCB table
  it came from.
- **The geometry column is always `geom`** in every normalized layer.
- **`execute_sql` is read-only and sandboxed**: no INSERT/UPDATE/DELETE/DDL,
  no file readers, no HTTP URLs, no abs paths. Numeric literals > 10 M are
  rejected as DoS protection. For writes, use `add_field` / `update_field` /
  `annotate`.

## When the LLM should push back, not plough on

- If the user's request needs data **not in the catalog** AND a web search
  can't plausibly fill the gap, say so plainly. Name the missing data.
  Don't fabricate values or silently substitute a proxy without flagging it.
- If a tool response carries a `warning`, `hint`, `truncated`, or `capped_at`
  field, **surface it to the user** rather than proceeding as if the result
  were complete.
- If `execute_sql` returns 50 rows and `truncated=True`, either narrow the
  query or re-run with `result_name="..."` to materialize as a full layer.
- If the user's bbox / radius / expression yields 0 rows, tell them; don't
  silently proceed with an empty layer.
- Small rule of thumb: when in doubt, ask before inventing.

## If something goes wrong

Every error response carries `error`, `detail`, and `origin` fields — plus
`detail` is prefixed with `[server-side]`. **Treat these as coming from
the MCP server's host, not your local machine.** If you see
"Permission denied" / "Failed to create directory" / any path-related
error, do NOT try to mkdir, chmod, or inspect paths on the client
filesystem — those files live on a different machine. Surface the error
to the user verbatim and ask how to proceed.

Common codes:
- `unknown_dataset` — check `search_data()` first
- `sql_rejected` — validator blocked the SQL; `detail` names the rule
- `sql_failed` — DuckDB execution error; check column names / types
- `op_failed` — generic op error; `detail` explains
- `missing_arg` / `too_many_points` / `unsupported_operation` — client-input
  shape problems
- `session_expired` — idle > 30 min; `replay` carries the operation log
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


# ---------------------------------------------------------------------------
# Parameter aliases — accept a small set of likely-guessed synonyms for the
# canonical names. Each alias appears in the tool's signature (so it shows up
# in the tool schema that clients see) but is immediately coalesced into the
# canonical name at the top of the tool body. ONE place to manage:
#
#   describe_dataset  ← id          = dataset_id
#   load              ← id          = dataset_id
#   load_many         ← ids, datasets = dataset_ids
#
# Add new aliases only when an LLM observably mis-guesses the canonical name.
# Don't add speculative aliases — two names for one parameter is clutter.
# ---------------------------------------------------------------------------


def _pick(*vals):
    """Return the first non-None value (for coalescing alias parameters)."""
    for v in vals:
        if v is not None:
            return v
    return None


def _checkpoint_hint_for_layer(sess: Session, layer: str) -> str:
    """Tip the LLM about checkpoint state for this specific layer."""
    from .operations import _reversible_for_layer
    covering = _reversible_for_layer(sess, layer)
    if covering:
        n = covering[0]
        return (
            f"Mutation on '{layer}' is reversible via checkpoint(s) {covering} — "
            f"call rollback('{n}') to undo, commit('{n}') to make permanent."
        )
    return (
        f"No checkpoint covers '{layer}' — this mutation is not reversible. "
        "Call checkpoint('name') or checkpoint('name', layers=['" + layer + "']) first."
    )


def _session(ctx: Context | None) -> Session:
    """Resolve the per-MCP-connection session. If ctx is None (stdio boot time)
    we fall back to a process-wide 'default' session. For streamable-HTTP each
    client request carries a session id that FastMCP threads through Context."""
    sid = getattr(ctx, "session_id", None) if ctx else None
    return REGISTRY.get_or_create(sid or "default")


# Stamp every error with a marker that identifies it as coming from the MCP
# server (remote) rather than the client's local environment. This prevents
# client LLMs from misdiagnosing e.g. "Permission denied" as a local
# filesystem issue and trying to mkdir/chmod on their own host.
_SERVER_ORIGIN = "geodata-mcp server (remote host)"
_ERR_PREFIX = "[server-side] "


def _server_error(kind: str, detail: str, **extra) -> dict:
    payload = {
        "error": kind,
        "detail": detail if detail.startswith(_ERR_PREFIX) else _ERR_PREFIX + detail,
        "origin": _SERVER_ORIGIN,
        "note": "This error originated inside the MCP server, not on the "
                "client/local machine. Do not try to mkdir/chmod/debug paths "
                "locally — any filesystem references are on the server's host.",
    }
    payload.update(extra)
    return payload


def _error_response(e: Exception) -> dict:
    if isinstance(e, SessionExpired):
        return _server_error("session_expired", str(e), replay=e.replay_info)
    if isinstance(e, SqlError):
        return _server_error("sql_rejected", str(e))
    if isinstance(e, OpError) or isinstance(e, LoadError):
        return _server_error("op_failed", str(e))
    return _server_error(type(e).__name__, str(e))


# ---------- helpers ----------

def _dataset_summary(d: DatasetEntry, verbose: bool = True) -> dict:
    """Dataset metadata. `verbose=True` includes the full attribute schema
    (name/type/description/sample values). `verbose=False` strips attributes —
    call describe_dataset(id) for the full picture once you know what you
    want to load."""
    base = {
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
    }
    if verbose:
        base["attributes"] = [
            {
                "name": a.name, "type": a.type,
                "description": a.description_sv,
                "description_en": a.description_en,
                "sample_values": a.sample_values,
            }
            for a in d.attributes
        ]
    else:
        base["n_attributes"] = len(d.attributes)
    return base


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
def search_data(query: str = "", limit: int = 20, verbose: bool = False) -> dict:
    """Fuzzy-search the catalog of locally available datasets.

    Default is compact output (id, name, description, coverage, temporal, license)
    to avoid flooding context. Call `describe_dataset(id)` for the full attribute
    schema of a specific dataset, or pass `verbose=True` here to get everything
    for every match (can be 20–50 KB).

    Args:
        query: Free-text search (Swedish or English). Empty string returns all
               datasets up to `limit`.
        limit: Max matches to return (default 20).
        verbose: If True, include full attribute schema (name/type/description/
                 sample values) per dataset. Default False for brevity.

    Returns: list of dataset summaries + total catalog size.
    """
    hits = CATALOG.search(query, limit=max(1, min(int(limit), 200)))
    return {
        "query": query,
        "total_in_catalog": len(CATALOG.all()),
        "verbose": verbose,
        "results": [
            {**_dataset_summary(d, verbose=verbose), "match_score": round(score, 1)}
            for d, score in hits
        ],
        "hint": (None if verbose else
                 "compact mode — call describe_dataset(id) for attribute schema "
                 "of the one you want to load"),
    }


@mcp.tool(annotations=_READ_ONLY)
def describe_dataset(
    dataset_id: str | None = None,
    id: str | None = None,  # alias for dataset_id
) -> dict:
    """Full metadata for a single catalog dataset — id, descriptions, coverage,
    temporal range, geometry type, feature count, CRS, publisher, license, and
    the complete attribute schema (column name, type, bilingual description,
    sample values).

    Use this after `search_data` when you need to understand a dataset's
    columns before loading it.

    Args:
        dataset_id: catalog id (canonical). Also accepts `id` as an alias.
    """
    dataset_id = _pick(dataset_id, id)
    if not dataset_id:
        return _server_error("missing_arg", "describe_dataset requires `dataset_id` (or alias `id`).")
    entry = CATALOG.get(dataset_id)
    if entry is None:
        return _server_error(
            "unknown_dataset",
            f"dataset_id '{dataset_id}' is not in the catalog on this server.",
            dataset_id=dataset_id,
            hint="call search_data() to list datasets — id spelling matters",
        )
    return _dataset_summary(entry, verbose=True)


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
    dataset_id: str | None = None,
    id: str | None = None,  # alias for dataset_id
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

    Args:
        dataset_id: catalog id (canonical). Also accepts `id` as an alias.

    Returns layer summary: name, feature_count, bbox, attribute schema, provenance.
    """
    dataset_id = _pick(dataset_id, id)
    if not dataset_id:
        return _server_error("missing_arg", "load requires `dataset_id` (or alias `id`).")
    entry = CATALOG.get(dataset_id)
    if entry is None:
        return _server_error(
            "unknown_dataset",
            f"dataset_id '{dataset_id}' is not in the catalog on this server.",
            dataset_id=dataset_id,
            hint="Call search_data() to list available datasets.",
        )
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
                return _server_error("missing_arg", "clip requires `layer` and `by_layer`.")
            meta = spatial_clip(sess, layer, by_layer, result_name=result_name)
        elif operation == "select_by_location":
            if not layer or not by_layer:
                return _server_error("missing_arg", "select_by_location requires `layer` and `by_layer`.")
            meta = spatial_select_by_location(
                sess, layer, by_layer,
                predicate=predicate,
                distance_m=distance_m,
                result_name=result_name,
            )
        elif operation == "intersect":
            if not a_layer or not b_layer:
                return _server_error("missing_arg", "intersect requires `a_layer` and `b_layer`.")
            meta = spatial_intersect(sess, a_layer, b_layer, result_name=result_name)
        elif operation == "buffer":
            if not layer or distance_m is None:
                return _server_error("missing_arg", "buffer requires `layer` and `distance_m`.")
            meta = spatial_buffer(sess, layer, float(distance_m), result_name=result_name)
        elif operation == "centroid":
            if not layer:
                return _server_error("missing_arg", "centroid requires `layer`.")
            meta = spatial_centroid(sess, layer, result_name=result_name)
        elif operation == "dissolve":
            if not layer:
                return _server_error("missing_arg", "dissolve requires `layer`.")
            meta = spatial_dissolve(sess, layer, by_columns=by_columns, result_name=result_name)
        elif operation == "convex_hull":
            if not layer:
                return _server_error("missing_arg", "convex_hull requires `layer`.")
            meta = spatial_convex_hull(sess, layer, aggregate=aggregate, result_name=result_name)
        else:
            return _server_error(
                "unsupported_operation",
                f"operation '{operation}' is not supported by the server.",
                operation=operation,
                supported=list(SpatialOp.__args__),
            )
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
        return f"[server-side error from {_SERVER_ORIGIN}] {type(e).__name__}: {e}"


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
        return _server_error("sql_rejected", str(e))
    except SessionExpired as e:
        return _error_response(e)
    except OpError as e:
        return _server_error("sql_failed", str(e))


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

    Use this when you bring data that isn't in the catalog — e.g. a manually
    curated lookup table, a transcription of external research, a simulated
    result — to join with catalog layers. The data is materialized in the
    session's DuckDB instance so all other tools (filter, spatial, stats,
    execute_sql, sources) can use it.

    **`source` is mandatory** — every row of the resulting layer carries a
    `source` attribute so the origin survives filtering, joining, and
    export. You can provide provenance two ways:

      - Top-level `source="Booli.se 2026-03 scrape"` — broadcast to every
        row. Use when all rows share one origin.
      - Per-row `source` field inside each dict — use when rows come from
        different origins (some from hitta.se, some from web search). The
        top-level `source` then fills gaps for rows that omit it.

    The call fails if neither form is provided. Short, specific source
    strings are best ("SL.se timetable 2026-04", "manual count 2026-04-19"),
    not generic ones like "the internet" or "web search".

    Args:
        name: Desired layer name. Collisions are suffixed (`_2`, `_3`, …).
        data: Up to 1,000 rows. List of dicts; each dict is one row with
              identical keys. Values may be any JSON-serializable scalar.
              A `source` key on each dict is preserved; otherwise the
              top-level `source` is auto-added.
        source: Required unless every row already has its own `source`.
                Short free-text description of where you got this data.
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

    The returned URL is absolute when PUBLIC_URL is configured, otherwise
    relative (`/exports/<token>/<filename>`). Links auto-expire after 24 h.
    """
    try:
        out = op_export(_session(ctx), layer, fmt=format)
        if "url" in out:
            out["url"] = _abs_url(out["url"])
        return out
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
        return f"[server-side error from {_SERVER_ORIGIN}] {e}"
    meta = sess.layers.get(layer)
    if meta is None:
        return (f"[server-side error from {_SERVER_ORIGIN}] unknown layer "
                f"'{layer}' in this session. Available: {list(sess.layers)}")

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
            return (f"[server-side error from {_SERVER_ORIGIN}] "
                    "WHERE expression cannot contain ';'")
        where_sql = f" WHERE {where}"
    sql = (
        f"SELECT {', '.join(select_cols)} FROM {_qi(layer)}"
        f"{where_sql} LIMIT {n} OFFSET {int(offset)}"
    )
    try:
        rows = sess.conn.execute(sql).fetchall()
        col_names = [d[0] for d in sess.conn.description]
    except Exception as e:
        return f"[server-side error from {_SERVER_ORIGIN}] {type(e).__name__}: {e}"

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
    sess.bump_version()
    return {
        "title": title,
        "viewer_url": _abs_url(f"/view/{sess.id}"),
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
    dataset_ids: list[str] | None = None,
    ids: list[str] | None = None,        # alias for dataset_ids
    datasets: list[str] | None = None,   # alias for dataset_ids
    bbox_3011: list[float] | None = None,
    limit: int | None = None,
    ctx: Context | None = None,
) -> dict:
    """Bulk-load several catalog datasets in one call. Identical semantics to
    `load` but applied across a list. The same bbox/limit apply to every
    dataset in the list — use single `load` calls if you need per-dataset
    arguments.

    Args:
        dataset_ids: list of catalog ids (canonical). Also accepts `ids` or
                     `datasets` as aliases.

    Returns: a list of summaries, plus a list of `errors` (per-dataset).
    """
    dataset_ids = _pick(dataset_ids, ids, datasets)
    if not dataset_ids:
        return _server_error(
            "missing_arg",
            "load_many requires `dataset_ids` (or alias `ids` / `datasets`).",
        )
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
        out["hint"] = _checkpoint_hint_for_layer(sess, layer)
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
        out["hint"] = _checkpoint_hint_for_layer(sess, layer)
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
        out["hint"] = _checkpoint_hint_for_layer(sess, layer)
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
        out["hint"] = _checkpoint_hint_for_layer(sess, layer)
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
            return _server_error(
                "missing_arg",
                "first call requires `layer`; subsequent calls use `cursor`.",
            )
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
    columns: list[str] | None = None,
    per_layer_limit: int = 3,
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
                Unknown names are reported in `unknown_layers`.
        columns: optional attribute subset to return per feature (keeps output
                 small when you only need a name/id).
        per_layer_limit: 1..25, default 3 (kept small to limit context bloat —
                         raise explicitly if you need more).
    """
    try:
        sess = _session(ctx)
        return op_inspect_location(
            sess, x_3011, y_3011,
            radius_m=radius_m, layers=layers,
            columns=columns,
            per_layer_limit=per_layer_limit,
        )
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_READ_ONLY)
def inspect_locations(
    points: list[dict],
    radius_m: float = 100.0,
    layers: list[str] | None = None,
    columns: list[str] | None = None,
    per_layer_limit: int = 3,
    ctx: Context | None = None,
) -> dict:
    """Batch variant of `inspect_location`: "what's near each of these points?"
    in one call.

    Args:
        points: list of `{"id": str|int, "x_3011": float, "y_3011": float}` dicts.
                If `id` is omitted, the list index is used.
        radius_m, layers, columns, per_layer_limit: same as inspect_location.

    Cap: 500 points per call.

    Returns: `{points: [{id, x_3011, y_3011, results: [{layer, features}, ...]}, ...],
               unknown_layers: [...], layers_considered: N}`.
    """
    try:
        sess = _session(ctx)
        if len(points) > 500:
            return _server_error(
                "too_many_points",
                f"got {len(points)}, cap is 500. Batch into smaller calls.",
            )
        return op_inspect_locations(
            sess, points,
            radius_m=radius_m, layers=layers,
            columns=columns, per_layer_limit=per_layer_limit,
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
        out["hint"] = _checkpoint_hint_for_layer(sess, name)
        return out
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_SAFE_MUTATION)
def rename_layer(old: str, new: str, ctx: Context | None = None) -> dict:
    """Rename a layer. Reversible inside an active checkpoint."""
    try:
        sess = _session(ctx)
        out = op_rename_layer(sess, old, new)
        out["hint"] = _checkpoint_hint_for_layer(sess, new)
        return out
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_IDEMPOTENT_MUTATION)
def hide(
    layers: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Hide layers in the viewer. Inverse of `show`. With no args, hides all.

    Args:
        layers: layer names to hide. None/empty → hide all.
    """
    try:
        sess = _session(ctx)
        out = op_hide_layers(sess, layers)
        out["viewer_url"] = _abs_url(f"/view/{sess.id}")
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
def checkpoint(
    name: str,
    layers: list[str] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Create a named checkpoint. Subsequent in-place mutations (`add_field`,
    `update_field`, `drop_field`, `annotate`, `drop_layer`, `rename_layer`)
    are snapshotted so that `rollback(name)` can undo them. `commit(name)`
    discards the snapshots and makes the mutations permanent.

    Scope:
        layers=None (default): covers **every** layer in the session. Any
            in-place mutation is tracked.
        layers=["a", "b"]: covers only those layers. Mutations to other
            layers are NOT snapshotted and can't be rolled back via this
            checkpoint — use a separate scoped checkpoint for them.

    Multiple checkpoints can be active simultaneously. A mutation covered by
    more than one active checkpoint is snapshotted for each. Storage cost is
    column-scoped (O(changed columns × rows)), not layer-wide.
    """
    try:
        return op_checkpoint(_session(ctx), name, layers=layers)
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
            "version": s.version,
            "visible_layers": s.visible_layers,
            "layers": {n: _layer_summary(s, n) for n in s.visible_layers},
        })

    async def api_version(request):
        """Tiny endpoint for viewer to poll — just the session's version
        counter. The viewer diffs on it to decide whether to re-fetch."""
        sid = request.path_params["session_id"]
        s = REGISTRY.get(sid)
        if s is None:
            return JSONResponse({"error": "unknown_or_expired_session"},
                                status_code=404)
        return JSONResponse({
            "session_id": s.id,
            "version": s.version,
            "visible_layers": s.visible_layers,
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
        """Emit a layer as a GeoJSON FeatureCollection.

        Streams features in chunks rather than building one giant JSON value,
        which OOMs the 256 MB per-session DuckDB limit for large layers
        (observed with 79 k buildings). Per-feature JSON is cheap; the
        aggregate is the problem.
        """
        from starlette.responses import StreamingResponse
        sid = request.path_params["session_id"]
        layer = request.path_params["layer"]
        s = REGISTRY.get(sid)
        if s is None or layer not in s.layers:
            return JSONResponse({"error": "unknown_layer_or_session"}, status_code=404)
        meta = s.layers[layer]
        geom_col = meta.attributes.get("__geom_col__") or ""
        if not geom_col:
            return JSONResponse({"type": "FeatureCollection", "features": []})
        cols = [c for c in meta.attributes if not c.startswith("__") and c != geom_col]
        props_struct = ", ".join(f"'{c}', {_qi(c)}" for c in cols) or "'_', NULL"

        # Build the per-feature JSON on the DuckDB side but keep them as
        # individual rows so the aggregator doesn't hold them all at once.
        per_feature_sql = f"""
            SELECT json_object(
                'type', 'Feature',
                'properties', json_object({props_struct}),
                'geometry', ST_AsGeoJSON(ST_Transform({_qi(geom_col)}, 'EPSG:3011', 'EPSG:4326', true))::JSON
            )::VARCHAR AS feature_json
            FROM {_qi(layer)}
        """
        CHUNK = 2000  # features per fetch — keeps per-iteration alloc bounded

        async def stream():
            try:
                cur = s.conn.execute(per_feature_sql)
            except Exception as e:
                # Can't start streaming a header + error — emit an empty FC.
                yield ('{"type":"FeatureCollection","features":[],'
                       '"error":"geojson_query_failed",'
                       f'"detail":{json.dumps(str(e))}' + '}').encode("utf-8")
                return
            yield b'{"type":"FeatureCollection","features":['
            first = True
            try:
                while True:
                    rows = cur.fetchmany(CHUNK)
                    if not rows:
                        break
                    parts = []
                    for (fj,) in rows:
                        if fj is None:
                            continue
                        if first:
                            first = False
                        else:
                            parts.append(",")
                        parts.append(fj)
                    if parts:
                        yield ("".join(parts)).encode("utf-8")
            except Exception as e:
                # Close the array and include a trailing error sidecar.
                yield (f'],"error":"geojson_stream_failed",'
                       f'"detail":{json.dumps(str(e))}' + '}').encode("utf-8")
                return
            yield b']}'

        return StreamingResponse(stream(), media_type="application/json")

    # FastMCP's http_app provides a /mcp route AND a lifespan that starts the
    # streamable-http session manager. We must (a) include its routes directly
    # (Mount double-prefixes the path) and (b) propagate its lifespan.
    mcp_app = mcp.http_app(transport="http")

    routes = [
        *mcp_app.routes,
        Route("/view/{session_id}", view_index),
        Route("/api/{session_id}/visible_layers", api_visible),
        Route("/api/{session_id}/version", api_version),
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
