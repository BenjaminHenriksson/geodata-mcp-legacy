"""FastMCP server exposing the Geodata MCP tool surface.

Run stdio (Claude Desktop / mcp CLI):
    uv run python -m geodata_mcp

Run HTTP (deployment, also serves the viewer at /view/{session_id}):
    uv run python -m geodata_mcp --http --port 8000
"""
from __future__ import annotations

import argparse
import functools
import os
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
    decode_unicode_escapes,
    add_field as op_add_field,
    annotate as op_annotate,
    batch_iterate as op_batch_iterate,
    checkpoint as op_checkpoint,
    classify as op_classify,
    commit as op_commit,
    create_layer as op_create_layer,
    drop_field as op_drop_field,
    drop_layer as op_drop_layer,
    execute_sql as op_execute_sql,
    export_and_cite as op_export_and_cite,
    export_layer as op_export,
    export_layers as op_export_layers,
    filter_layer,
    hide_layers as op_hide_layers,
    inspect_location as op_inspect_location,
    inspect_locations as op_inspect_locations,
    list_layers as op_list_layers,
    rename_layer as op_rename_layer,
    reverse_geocode as op_reverse_geocode,
    rollback as op_rollback,
    set_notes as op_set_notes,
    sources as op_sources,
    spatial_buffer, spatial_centroid, spatial_clip, spatial_convex_hull,
    spatial_dissolve, spatial_intersect, spatial_select_by_location,
    top_n as op_top_n,
    update_field as op_update_field,
)
from .session import REGISTRY, Session, SessionExpired

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_URL = os.environ.get("GEODATA_PUBLIC_URL", "").rstrip("/")


def _abs_url(path: str) -> str:
    if PUBLIC_URL and path.startswith("/"):
        return f"{PUBLIC_URL}{path}"
    return path


CATALOG = Catalog.load()

SERVER_INSTRUCTIONS = """\
# Geodata MCP — Stockholm open geodata

Swedish municipal geodata (SCB DeSO + Stockholm SBK Stadskarta + OSM
addresses) for LLM-driven analysis. Session-scoped DuckDB + Spatial,
all layers in EPSG:3011 (SWEREF 99 18 00; Stockholm-local metres),
filtered to Stockholm kommun (kommunkod `0180`). 65 datasets.

## Tools (13)

1. `catalog(query?, id?, verbose?)` — fuzzy-search datasets; pass `id` for full attribute schema.
2. `geocode(op, ...)` — `"forward"` (name→coords), `"reverse"` (coords→admin area), `"bbox"` (name→bbox).
3. `load(op, ...)` — `"catalog"` pulls 1..N datasets by id; `"inline"` injects LLM-provided rows (`source` mandatory).
4. `execute_sql(sql, ...)` — read-only DuckDB+Spatial. 30 s timeout. DDL/DML/file I/O rejected.
5. `derive(op, ...)` — new layer from existing: `"filter"`, `"top_n"`, `"clip"`, `"intersect"`, `"select_by_location"` (supports `center_3011`+`distance_m` for bare point+radius), `"buffer"`, `"centroid"`, `"dissolve"`, `"convex_hull"`.
6. `edit_field(op, layer, ...)` — expression-driven column mutations: `"add"`, `"update"`, `"drop"`, `"classify"`. Reversible inside a checkpoint.
7. `write_attributes(layer, values, ...)` — data-driven bulk per-feature attribute writes from a `{key: {attr: val, ...}}` dict (creates columns on the fly, keyed by `key_column`). Use when the LLM has specific per-row knowledge; use `edit_field(op="classify")` when a uniform CASE-WHEN applies.
8. `inspect(op, ...)` — `"layers"` (session inventory), `"rows"` (sample, with optional `include_rowid`), `"batch"` (cursor pagination), `"at"` (spatial "what's here" for 1..500 points).
9. `layer(op, ...)` — visibility + lifecycle: `"show"`, `"hide"`, `"rename"`, `"drop"`, `"set_notes"`.
10. `export(layers, format, cite?)` — data-only: gpkg/geojson/csv/parquet. `cite=True` bundles provenance markdown.
11. `render_map(layers, ...)` — server-rendered styled PNG with Carto Positron basemap underlay. Honours the current `layer(op="show")` style.
12. `sources(layer?)` — provenance markdown for citations.
13. `checkpoint(op, name, ...)` — `"create"` a savepoint, `"rollback"` to undo, `"commit"` to make permanent.

## Core workflow

```
catalog(query="income")
geocode(op="forward", name="Tekniska nämndhuset")  # → (152699, 6579781)
load(op="catalog", dataset_ids=["deso_2025", "scb_income_structure"])
execute_sql(sql='''
  WITH here AS (
    SELECT desokod FROM deso_2025
    WHERE ST_Contains(geom, ST_Point(152699, 6579781))
  )
  SELECT år, value AS mean_tkr
  FROM scb_income_structure i JOIN here h ON i.desokod_2025 = h.desokod
  WHERE tabellinnehåll='Medelvärde för samtliga, tkr' AND kön='totalt'
    AND value IS NOT NULL
  ORDER BY år DESC LIMIT 3
''')
sources()
```

## Reversible in-place mutations via checkpoint

`edit_field` and `layer` ops (rename/drop) are reversible if wrapped in
a checkpoint:

```
checkpoint(op="create", name="tag", layers=["buildings"])
edit_field(op="classify", layer="buildings", name="era",
           rules=[{"when": "byggar<1940", "then": "historic"},
                  {"when": "byggar>=2000", "then": "modern"}],
           default="mid")
# inspect; then either:
checkpoint(op="rollback", name="tag")    # undo
checkpoint(op="commit", name="tag")       # make permanent
```

Pass `layers=[...]` to scope the checkpoint; multiple can be active at
once (column-scoped snapshots, not full-layer copies).

## Canonical join keys across every SCB table

Every `scb_*` parquet carries: `desokod` (raw), `desokod_2025`
(bridges 2018→2025 grid changes via `deso_historical_changes`),
`regsokod`, `regso_name`, `kommunkod`, `kommun_name`. Prefer these
canonical columns over the raw `region` column — they're uniform
across all SCB tables and safe to join against DeSO polygons.

## Bulk enrichment

For layers too large to hold in one prompt (> ~500 features):

```
checkpoint(op="create", name="tag", layers=["sbk_buildings"])
out = inspect(op="batch", layer="sbk_buildings",
              columns=["id","name","byggar"], batch_size=500)
while True:
    tags = {row["rowid"]: {"era": ..., "confidence": ...} for row in out["rows"]}
    write_attributes(layer="sbk_buildings", values=tags)
    if out.get("exhausted"): break
    out = inspect(op="batch", cursor=out["next_cursor"])
checkpoint(op="commit", name="tag")
```

For ≤ ~500 features, skip the loop: one `inspect(op="rows", n=500,
include_rowid=True)` then one `write_attributes(layer="...", values=...)`.
`inspect(op="rows")` returns both a `table_md` and a structured
`rows` list — feed the `rows` list directly into the `values` dict
builder.

## Provenance — mandatory on LLM-injected data

`load(op="inline")` REQUIRES `source` — either top-level (applied to
every row) or per-row `source` field in each dict. Rows inherit source
through filter/join/export. Short specific sources ("SL.se timetable
2026-04", "hitta.se lookup 2026-04-19") beat generic ones ("web
search").

Columns written by `edit_field` record the LLM author when you pass
`model="claude-opus-4-7"` (or similar) — exported columns trace back
to their author.

## Every mutation takes `description`

`load`, `derive`, `edit_field`, `layer`, `export`, `execute_sql`, and
`checkpoint` all accept `description: str` — a one-sentence rationale
shown in the viewer's audit panel. Treat it like a commit message:
short, specific, user-framed ("filter to pre-war buildings for
historical-core analysis") not implementation-framed ("call ST_Buffer
on foo"). Operations without a description are visibly flagged as
undocumented.

## Never guess a place from raw coordinates

EPSG:3011 numbers like `(154706, 6572108)` do NOT tell you "this is
Mariehäll". Call `geocode(op="reverse", x_3011=..., y_3011=...)` to
get the containing Stadsdel/Distrikt/Kvarter from SBK's
administrative layer. Cite those names only — never invent. Same rule
for `inspect(op="at")` results.

## Coordinate reference system

All session layers in EPSG:3011. Bounding boxes, distances, buffers
are metres in EPSG:3011. `geocode` returns EPSG:3011 coords. Exports:
geojson reprojects to EPSG:4326, gpkg stays native EPSG:3011.

## Footguns

- **SCB privacy suppression** — small-population DeSOs have NULL
  values. Always `WHERE value IS NOT NULL` before aggregating.
- **DeSO 2018 → 2025 grid changes** — use `desokod_2025` for joins,
  not the raw `region` column.
- **Swedish attribute names** — `byggar` = year built, `antal` =
  count, `KATEGORI`/`GRUPP` = category/group. Every SCB value column
  is named `value`.
- **Geometry column is always `geom`.**
- **`execute_sql` is read-only** — no DDL/DML, no HTTP/file readers,
  no abs paths. Numeric literals > 10M are rejected as DoS
  protection. For writes use `edit_field`.

## When to push back, not plough on

- If the user needs data not in the catalog and web search can't
  plausibly fill it, say so plainly. Don't fabricate.
- If a tool response has `warning`/`hint`/`truncated`/`capped_at`
  fields, surface them rather than proceeding as complete.
- If a query yields 0 rows, tell the user — don't silently proceed.

## Errors are server-side, not local

Every error response has `error`, `detail` (prefixed
`[server-side]`), and `origin` fields. These live on the MCP host —
do NOT try to mkdir/chmod/debug paths on your local machine based on
them. Surface the error verbatim to the user.

Common codes: `unknown_dataset`, `sql_rejected`, `sql_failed`,
`op_failed`, `missing_arg`, `too_many_points`,
`unsupported_operation`, `session_expired` (carries a `replay` block
with the operation log so you can reconstruct).
"""


mcp = FastMCP("geodata-mcp", instructions=SERVER_INSTRUCTIONS)


# ---------------------------------------------------------------------------
# Common annotation shapes.
# ---------------------------------------------------------------------------
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_SAFE_MUTATION = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=False, openWorldHint=False,
)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _session(ctx: Context | None) -> Session:
    """Resolve the per-connection session; fall back to a shared 'default'
    session for stdio boot / non-HTTP contexts."""
    sid = None
    if ctx is not None:
        try:
            sid = ctx.session_id
        except (AttributeError, RuntimeError):
            # FastMCP raises RuntimeError from the session_id property when
            # no request context is bound (e.g. direct call_tool() in tests).
            sid = None
    return REGISTRY.get_or_create(sid or "default")


def _audited(tool_name: str):
    """Wrap an MCP tool body in Session.audit_context. Pulls `description`
    and `ctx` from kwargs; the wrapped function still resolves
    _session(ctx) itself (idempotent)."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            ctx = kwargs.get("ctx")
            description = kwargs.get("description", "") or ""
            try:
                sess = _session(ctx)
            except Exception:
                return fn(*args, **kwargs)
            audit_args = {k: v for k, v in kwargs.items()
                          if k != "ctx" and not callable(v)}
            with sess.audit_context(tool_name, description=description,
                                    args=audit_args):
                return fn(*args, **kwargs)
        return wrapper
    return deco


_SERVER_ORIGIN = "geodata-mcp server (remote host)"
_ERR_PREFIX = "[server-side] "


def _server_error(kind: str, detail: str, **extra) -> dict:
    payload = {
        "error": kind,
        "detail": detail if detail.startswith(_ERR_PREFIX) else _ERR_PREFIX + detail,
        "origin": _SERVER_ORIGIN,
        "note": "This error originated inside the MCP server, not on the "
                "client/local machine. Do not try to mkdir/chmod/debug "
                "paths locally — filesystem references are on the server's "
                "host.",
    }
    payload.update(extra)
    return payload


def _error_response(e: Exception) -> dict:
    if isinstance(e, SessionExpired):
        return _server_error("session_expired", str(e), replay=e.replay_info)
    if isinstance(e, SqlError):
        return _server_error("sql_rejected", str(e))
    if isinstance(e, (OpError, LoadError)):
        return _server_error("op_failed", str(e))
    return _server_error(type(e).__name__, str(e))


def _attach_hint(out: dict, sess: Session, layer: str) -> dict:
    """Attach a reversibility hint to `out` iff a covering checkpoint
    exists for `layer`."""
    from .operations import _reversible_for_layer
    covering = _reversible_for_layer(sess, layer)
    if covering:
        n = covering[0]
        out["hint"] = (
            f"Mutation on '{layer}' is reversible via checkpoint(s) {covering} — "
            f"call checkpoint(op='rollback', name='{n}') to undo, "
            f"checkpoint(op='commit', name='{n}') to make permanent."
        )
    else:
        out.pop("hint", None)
    return out


def _dataset_summary(d: DatasetEntry, verbose: bool = True) -> dict:
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
    if d.dedupe_hint:
        base["dedupe_hint"] = d.dedupe_hint
    if verbose:
        base["attributes"] = [
            {"name": a.name, "type": a.type,
             "description": a.description_sv,
             "description_en": a.description_en,
             "sample_values": a.sample_values}
            for a in d.attributes
        ]
    else:
        base["n_attributes"] = len(d.attributes)
    return base


_QUICK_STATS_CAP = int(os.environ.get("GEODATA_QUICK_STATS_CAP", "200000"))


def _quick_stats_and_sample(sess: Session, name: str, m) -> dict:
    """Best-effort stats + 3-row sample for layers under the stats cap."""
    if m.feature_count > _QUICK_STATS_CAP:
        return {}
    cols = [c for c in m.attributes if not c.startswith("__")]
    if not cols:
        return {}
    geom_col = m.attributes.get("__geom_col__") or ""
    attr_cols = [c for c in cols if c != geom_col]
    from .operations import is_numeric_sql_type
    out: dict = {}
    qname = _qi(name)
    numeric_cols = [c for c in attr_cols if is_numeric_sql_type(m.attributes[c])]
    if numeric_cols:
        parts = []
        for c in numeric_cols:
            q = _qi(c)
            parts += [
                f"COUNT({q}) AS \"{c}__count\"",
                f"COUNT(*) - COUNT({q}) AS \"{c}__nulls\"",
                f"MIN({q}) AS \"{c}__min\"",
                f"AVG({q}) AS \"{c}__mean\"",
                f"MAX({q}) AS \"{c}__max\"",
            ]
        sql = f"SELECT {', '.join(parts)} FROM {qname}"
        try:
            row = sess.conn.execute(sql).fetchone()
            col_names = [d[0] for d in sess.conn.description]
            stats: dict[str, dict] = {}
            for k, v in zip(col_names, row):
                col, _, key = k.partition("__")
                stats.setdefault(col, {})[key] = v
            def _informative(s: dict) -> bool:
                if (s.get("count") or 0) == 0:
                    return False
                mn, mx = s.get("min"), s.get("max")
                if mn is not None and mx is not None and mn == mx:
                    return False
                return True
            stats = {c: s for c, s in stats.items() if _informative(s)}
            if stats:
                out["quick_stats"] = stats
        except Exception:
            pass
    if attr_cols:
        sql = f"SELECT {', '.join(_qi(c) for c in attr_cols)} FROM {qname} LIMIT 3"
        try:
            rows = sess.conn.execute(sql).fetchall()
            out["sample"] = [dict(zip(attr_cols, r)) for r in rows]
        except Exception:
            pass
    return out


def _layer_summary(sess: Session, name: str) -> dict:
    m = sess.layers[name]
    payload: dict = {
        "name": m.name,
        "feature_count": m.feature_count,
        "geometry_type": m.geometry_type,
        "bbox_3011": list(m.bbox) if m.bbox else None,
        "attributes": {k: v for k, v in m.attributes.items() if not k.startswith("__")},
        "provenance": [
            {"dataset_id": s.dataset_id, "source_name": s.source_name,
             "publisher": s.publisher, "license": s.license,
             "url": s.url, "retrieved": s.retrieved,
             "llm_sourced": s.llm_sourced}
            for s in m.provenance
        ],
    }
    if m.notes:
        payload["notes"] = m.notes
    if m.column_provenance:
        payload["column_provenance"] = dict(m.column_provenance)
    payload.update(_quick_stats_and_sample(sess, name, m))
    return payload


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _md_cell(v) -> str:
    if v is None:
        return ""
    s = str(v).replace("\n", " ").replace("|", "\\|")
    return s if len(s) < 80 else s[:77] + "..."


# ---------------------------------------------------------------------------
# Tools (11).
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY)
def catalog(
    query: str = "",
    id: str | None = None,
    verbose: bool = False,
    limit: int = 20,
) -> dict:
    """Catalog search + describe. Pass `id` for full metadata of a single
    dataset (attribute schema, sample values, descriptions); otherwise
    fuzzy-search the 65-dataset catalog.

    Compact search mode (`verbose=False`) returns id/name/description/
    coverage/temporal/license per hit. `verbose=True` includes full
    attribute schema for every hit (can be 20–50 KB). Prefer
    `catalog(id=...)` to zoom in on one dataset after a compact search.

    Args:
        query: Free-text search (Swedish or English). Empty = list all up to `limit`.
        id: If set, return full metadata for that dataset id (ignoring `query`).
        verbose: If True and `id` is None, include attributes for every hit.
        limit: Max matches (default 20, cap 200).
    """
    if id:
        entry = CATALOG.get(id)
        if entry is None:
            return _server_error(
                "unknown_dataset",
                f"dataset_id '{id}' is not in the catalog on this server.",
                dataset_id=id,
                hint="call catalog() with a query to list datasets — id spelling matters",
            )
        return _dataset_summary(entry, verbose=True)
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
                 "compact mode — call catalog(id='...') for full attribute "
                 "schema of the one you want to load"),
    }


@mcp.tool(annotations=_READ_ONLY)
def geocode(
    op: Literal["forward", "reverse", "bbox"],
    name: str | None = None,
    x_3011: float | None = None,
    y_3011: float | None = None,
    buffer_m: float = 0.0,
    limit: int = 5,
    all_kinds: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Place ↔ coordinate lookups, all directions.

    - `op="forward"` (requires `name`): place name / composite street+number
      → EPSG:3011 coordinates + bbox. Composite addresses are spatially
      paired with the nearest AdressText point. Stadsdel/Distrikt/Kvarter
      return polygon-backed bboxes. Pass `all_kinds=True` to keep separate
      rows for different GRUPP values (else deduped by name).
    - `op="reverse"` (requires `x_3011`, `y_3011`): EPSG:3011 point →
      containing Stadsdel / Stadsdelsnämndsområde / Distrikt / Kvarter /
      Kommun polygons (+ `by_kategori` grouping). **Always use this
      rather than guessing neighborhoods from coordinates.**
    - `op="bbox"` (requires `name`): place name → best-match EPSG:3011
      bbox, optionally expanded by `buffer_m` on each side.
    """
    try:
        sess = _session(ctx)
    except SessionExpired as e:
        return _error_response(e)
    if op == "forward":
        if not name:
            return _server_error("missing_arg", "geocode op='forward' requires `name`.")
        matches = do_geocode(sess.conn, name, limit=limit, all_kinds=all_kinds)
        return {
            "query": name,
            "coverage": "Stockholm kommun",
            "matches": [
                {"name": m.name, "kind": m.grupp, "subkind": m.kategori,
                 "x_3011": m.x_3011, "y_3011": m.y_3011,
                 "bbox_3011": list(m.bbox_3011),
                 "score": round(m.score, 4)}
                for m in matches
            ],
        }
    if op == "reverse":
        if x_3011 is None or y_3011 is None:
            return _server_error(
                "missing_arg",
                "geocode op='reverse' requires `x_3011` and `y_3011`.",
            )
        admin_path = str(ROOT / "data/normalized/sbk/Adm_area.gpkg")
        try:
            return op_reverse_geocode(sess, float(x_3011), float(y_3011), admin_path)
        except Exception as e:
            return _error_response(e)
    if op == "bbox":
        if not name:
            return _server_error("missing_arg", "geocode op='bbox' requires `name`.")
        matches = do_geocode(sess.conn, name, limit=1)
        if not matches:
            return _server_error(
                "no_match",
                f"no geocode match within Stockholm coverage for '{name}'.",
                query=name,
            )
        m = matches[0]
        xmin, ymin, xmax, ymax = m.bbox_3011
        b = max(0.0, float(buffer_m))
        if b > 0:
            xmin -= b; ymin -= b; xmax += b; ymax += b
        return {
            "name": m.name, "kind": m.grupp, "subkind": m.kategori,
            "score": round(m.score, 4),
            "center_3011": [m.x_3011, m.y_3011],
            "bbox_3011": [xmin, ymin, xmax, ymax],
            "buffer_m": b,
        }
    return _server_error("unsupported_operation",
                         f"op={op!r} not supported",
                         supported=["forward", "reverse", "bbox"])


@mcp.tool(annotations=_SAFE_MUTATION)
@_audited("load")
def load(
    op: Literal["catalog", "inline"],
    dataset_ids: list[str] | None = None,
    data: list[dict] | None = None,
    source: str | None = None,
    geometry_column: str | None = None,
    crs: str = "EPSG:4326",
    bbox_3011: list[float] | None = None,
    where: str | None = None,
    intersect_layer: str | None = None,
    layer_name: str | None = None,
    limit: int | None = None,
    description: str = "",
    ctx: Context | None = None,
) -> dict:
    """Pull catalog datasets into the session OR inject LLM-provided rows.

    - `op="catalog"` (requires `dataset_ids`): load 1..N datasets.
      When exactly one id is given, per-dataset filters apply:
      `bbox_3011`, `where`, `intersect_layer`, `layer_name`.
      When multiple ids are given, only `bbox_3011` and `limit` apply
      (mirrors the old bulk-load semantics). Server cap: 100,000
      features per dataset.

    - `op="inline"` (requires `data`): inject LLM-provided rows as a
      new layer. Up to 1,000 rows. **`source` is MANDATORY** — either
      top-level (broadcast to all rows) or as a per-row `source` field.
      Rows carry `source` through filter/join/export for audit trail.
      If a column holds WKT strings, name it in `geometry_column`
      (parsed and reprojected from `crs` → EPSG:3011).

    Returns: `{loaded: [layer_summary, ...], errors: [...], n_loaded: N}`.
    """
    try:
        sess = _session(ctx)
    except SessionExpired as e:
        return _error_response(e)

    if op == "inline":
        if not data:
            return _server_error("missing_arg", "load op='inline' requires `data`.")
        try:
            meta = op_create_layer(
                sess, layer_name or "inline", data,
                source=source, geometry_column=geometry_column, crs=crs,
            )
        except Exception as e:
            return _error_response(e)
        return {
            "loaded": [_layer_summary(sess, meta.name)],
            "errors": [],
            "n_loaded": 1,
        }

    if op == "catalog":
        if not dataset_ids:
            return _server_error("missing_arg", "load op='catalog' requires `dataset_ids`.")
        bbox_tuple = tuple(bbox_3011) if bbox_3011 and len(bbox_3011) == 4 else None
        results, errors = [], []
        single = len(dataset_ids) == 1
        for did in dataset_ids:
            entry = CATALOG.get(did)
            if entry is None:
                errors.append({"dataset_id": did, "error": "unknown_dataset"})
                continue
            try:
                if single:
                    meta = load_dataset(
                        sess, entry,
                        bbox_3011=bbox_tuple, limit=limit, layer_name=layer_name,
                        where=where, intersect_layer=intersect_layer,
                    )
                else:
                    meta = load_dataset(sess, entry, bbox_3011=bbox_tuple, limit=limit)
                results.append(_layer_summary(sess, meta.name))
            except Exception as e:
                errors.append({"dataset_id": did, "error": type(e).__name__, "detail": str(e)})
        return {"loaded": results, "errors": errors, "n_loaded": len(results)}

    return _server_error("unsupported_operation",
                         f"op={op!r} not supported",
                         supported=["catalog", "inline"])


@mcp.tool(annotations=_SAFE_MUTATION)
@_audited("execute_sql")
def execute_sql(
    sql: str,
    description: str = "",
    result_name: str | None = None,
    geometry_column: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Run a validated, read-only SQL query against session layers.

    Only a single SELECT/WITH/UNION is accepted — DDL/DML is rejected
    by a sqlglot-based parser. DuckDB spatial functions are available.
    Session layers are referenced by their names as regular tables.

    Layer-vs-table decision:
      1. If `geometry_column` is passed, layer mode is forced.
      2. Else the tool runs DESCRIBE (sql) and promotes to layer if
         any column has type starting with `GEOMETRY`.
      3. Otherwise it returns up to 50 rows as a markdown table. If a
         column named `geom`/`geometry` exists but got demoted to
         BLOB, the response carries a `geometry_hint` telling you to
         retry with `geometry_column='...'`.

    30-second wall-clock timeout, 256 MB per-session memory.

    Args:
        sql: Read-only SQL. No semicolons, no DDL, no multi-statement.
        description: Free-text rationale stored in the audit log.
        result_name: Optional name if the query yields a layer.
        geometry_column: Explicit geometry-column hint.
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
@_audited("derive")
def derive(
    op: Literal["filter", "top_n", "clip", "intersect",
                "select_by_location", "buffer", "centroid",
                "dissolve", "convex_hull"],
    layer: str | None = None,
    by_layer: str | None = None,
    a_layer: str | None = None,
    b_layer: str | None = None,
    where: str | None = None,
    distance_m: float | None = None,
    by_columns: list[str] | None = None,
    aggregate: bool = False,
    predicate: Literal["intersects", "within", "contains", "dwithin"] = "intersects",
    center_3011: list[float] | None = None,
    by: str | None = None,
    n: int = 10,
    ascending: bool = False,
    result_name: str | None = None,
    description: str = "",
    ctx: Context | None = None,
) -> dict:
    """Produce a new layer from an existing one. Provenance inherits
    from the source.

    - `op="filter"` (layer, where): SQL WHERE → new layer. Supports
      spatial predicates on `geom` (e.g. `ST_DWithin(geom, ...)`).
    - `op="top_n"` (layer, by, n?, ascending?): filter + ORDER BY +
      LIMIT in one call. `by` is an SQL ordering expression (bare or
      with trailing `ASC`/`DESC`/`NULLS FIRST|LAST` — the trailing
      direction wins over `ascending`).
    - `op="clip"` (layer, by_layer): trim `layer`'s geometries to the
      union of `by_layer`'s. Geometries MODIFIED.
    - `op="intersect"` (a_layer, b_layer): geometric overlay, one row
      per intersecting pair.
    - `op="select_by_location"` (layer, by_layer, predicate) OR
      (layer, center_3011, distance_m): spatial WHERE — keep features
      of `layer` relating to another layer OR to a literal EPSG:3011
      point. Predicates: intersects / within / contains / dwithin
      (the latter needs `distance_m`). Use `center_3011=[x, y]` +
      `distance_m=N` to select within N metres of a coordinate
      without loading a separate point layer first.
    - `op="buffer"` (layer, distance_m): ST_Buffer in EPSG:3011 metres.
    - `op="centroid"` (layer): per-feature ST_Centroid.
    - `op="dissolve"` (layer, by_columns?): union geometries, grouped.
    - `op="convex_hull"` (layer, aggregate?): per-feature hull, or one
      aggregate hull for the whole layer with `aggregate=True`.

    Args:
        result_name: Optional name for the new layer (auto-generated if omitted).
        description: One-sentence rationale for the audit log.
    """
    try:
        sess = _session(ctx)
        if op == "filter":
            if not layer or not where:
                return _server_error("missing_arg",
                                     "derive op='filter' requires `layer` and `where`.")
            meta = filter_layer(sess, layer, where, result_name=result_name)
        elif op == "top_n":
            if not layer or not by:
                return _server_error("missing_arg",
                                     "derive op='top_n' requires `layer` and `by`.")
            meta = op_top_n(sess, layer, by, n=n, ascending=ascending,
                            result_name=result_name)
        elif op == "clip":
            if not layer or not by_layer:
                return _server_error("missing_arg",
                                     "derive op='clip' requires `layer` and `by_layer`.")
            meta = spatial_clip(sess, layer, by_layer, result_name=result_name)
        elif op == "intersect":
            if not a_layer or not b_layer:
                return _server_error("missing_arg",
                                     "derive op='intersect' requires `a_layer` and `b_layer`.")
            meta = spatial_intersect(sess, a_layer, b_layer, result_name=result_name)
        elif op == "select_by_location":
            if not layer:
                return _server_error(
                    "missing_arg",
                    "derive op='select_by_location' requires `layer`.",
                )
            if not by_layer and center_3011 is None:
                return _server_error(
                    "missing_arg",
                    "derive op='select_by_location' requires either `by_layer` "
                    "or `center_3011` + `distance_m`.",
                )
            if center_3011 is not None and distance_m is None:
                return _server_error(
                    "missing_arg",
                    "derive op='select_by_location' with `center_3011` "
                    "requires `distance_m`.",
                )
            meta = spatial_select_by_location(
                sess, layer, by_layer,
                predicate=predicate, distance_m=distance_m,
                center_3011=center_3011,
                result_name=result_name,
            )
        elif op == "buffer":
            if not layer or distance_m is None:
                return _server_error("missing_arg",
                                     "derive op='buffer' requires `layer` and `distance_m`.")
            meta = spatial_buffer(sess, layer, float(distance_m), result_name=result_name)
        elif op == "centroid":
            if not layer:
                return _server_error("missing_arg", "derive op='centroid' requires `layer`.")
            meta = spatial_centroid(sess, layer, result_name=result_name)
        elif op == "dissolve":
            if not layer:
                return _server_error("missing_arg", "derive op='dissolve' requires `layer`.")
            meta = spatial_dissolve(sess, layer, by_columns=by_columns,
                                    result_name=result_name)
        elif op == "convex_hull":
            if not layer:
                return _server_error("missing_arg", "derive op='convex_hull' requires `layer`.")
            meta = spatial_convex_hull(sess, layer, aggregate=aggregate,
                                       result_name=result_name)
        else:
            return _server_error(
                "unsupported_operation",
                f"op={op!r} not supported",
                supported=["filter", "top_n", "clip", "intersect",
                           "select_by_location", "buffer", "centroid",
                           "dissolve", "convex_hull"],
            )
    except Exception as e:
        return _error_response(e)
    return _layer_summary(sess, meta.name)


@mcp.tool(annotations=_SAFE_MUTATION)
@_audited("edit_field")
def edit_field(
    op: Literal["add", "update", "drop", "classify"],
    layer: str,
    name: str | None = None,
    expr: str | None = None,
    field_type: str | None = None,
    where: str | None = None,
    rules: list[dict] | None = None,
    default: str | None = None,
    description: str = "",
    ctx: Context | None = None,
) -> dict:
    """Expression-driven column mutations on a session layer — one SQL
    expression applied uniformly across all (or `where`-restricted)
    rows. Reversible inside an active `checkpoint(op='create', ...)`
    covering this layer.

    For **data-driven** bulk attribute writes (LLM-classified
    per-feature values from a `{key: {attr: val, ...}}` dict), use
    the dedicated `annotate` tool instead.

    - `op="add"` (name, expr, field_type?): new column computed from a
      SQL expression. Type inferred unless `field_type` is set
      (VARCHAR/DOUBLE/BIGINT/BOOLEAN/DATE/TIMESTAMP).
    - `op="update"` (name, expr, where?): overwrite an existing
      column's values; optional WHERE restriction.
    - `op="drop"` (name): remove a column. Refuses the geometry
      column (use `layer(op='drop', ...)` for the whole layer).
    - `op="classify"` (name, rules=[{when, then}], default?): CASE-WHEN
      shorthand adding a categorical column. Rules evaluated in order;
      first match wins.
    """
    try:
        sess = _session(ctx)
        if op == "add":
            if not name or not expr:
                return _server_error("missing_arg",
                                     "edit_field op='add' requires `name` and `expr`.")
            out = op_add_field(sess, layer, name, expr, field_type=field_type)
        elif op == "update":
            if not name or not expr:
                return _server_error("missing_arg",
                                     "edit_field op='update' requires `name` and `expr`.")
            out = op_update_field(sess, layer, name, expr, where=where)
        elif op == "drop":
            if not name:
                return _server_error("missing_arg",
                                     "edit_field op='drop' requires `name`.")
            out = op_drop_field(sess, layer, name)
        elif op == "classify":
            if not name or not rules:
                return _server_error("missing_arg",
                                     "edit_field op='classify' requires `name` and `rules`.")
            out = op_classify(sess, layer, name, rules=rules, default=default)
        else:
            return _server_error(
                "unsupported_operation",
                f"op={op!r} not supported",
                supported=["add", "update", "drop", "classify"],
            )
        _attach_hint(out, sess, layer)
        return out
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_SAFE_MUTATION)
@_audited("write_attributes")
def write_attributes(
    layer: str,
    values: dict | None = None,
    key_column: str = "rowid",
    dry_run: bool = False,
    model: str | None = None,
    description: str = "",
    ctx: Context | None = None,
) -> dict:
    """Bulk per-feature attribute writer — **data-driven** column
    updates with one authoritative value per row, keyed by
    `key_column`.

    Use this when the LLM has specific knowledge per row (read a
    sample, classify each individually, write the classifications
    back) rather than a single SQL expression covering all rows
    uniformly — for the latter use `edit_field(op="classify", ...)`.
    Creates columns on the fly if missing; also overwrites existing
    values. (Despite the historical "annotate" name this replaces,
    these are authoritative attribute writes, not footnotes.)

    Payload shape:
        values = {
            "<key>": {"era": "functionalist", "confidence": 0.9, "note": "..."},
            "<key>": {"era": "art-nouveau",    "confidence": 0.7, ...},
            ...
        }

    Columns are created on the fly if they don't exist (type inferred
    from the values: all-int → BIGINT, int/float mix → DOUBLE, bool →
    BOOLEAN, else VARCHAR). Up to 10,000 keys per call. Pair with
    `inspect(op="batch", ...)` for layers larger than you can reason
    about in one pass.

    The response reports both key-level matching
    (`keys_matched` / `keys_unmatched`) and row-level coverage
    (`rows_total` / `rows_with_any_annotation` /
    `rows_without_annotation`) — so you can distinguish "every key I
    sent hit a row" from "every row in the layer received a value".
    The two differ when your `values` dict covers only a subset.

    Args:
        layer: target layer.
        values: `{key_value: {attr_name: val, ...}}` — many features per call.
        key_column: column to match on. Default `"rowid"` (DuckDB
            pseudo-column, stable within a session). Use a declared
            key column when one exists (e.g. `NAMN`, `id`).
        dry_run: if True, preview coverage without writing. Returns
            `{dry_run: True, keys_matched, keys_unmatched,
            new_columns_would_create, ...}`. Use before committing
            large payloads to catch key_column mismatches.
        model: optional author id (`"claude-opus-4-7"`, `"gpt-5"`,
            etc.) stored in per-column provenance so exported columns
            can be traced to their author.

    Reversible inside a checkpoint (pre-image snapshotted once per
    column, not once per row).
    """
    try:
        sess = _session(ctx)
        if values is None:
            return _server_error("missing_arg",
                                 "write_attributes requires `values`.")
        out = op_annotate(sess, layer, values, key_column=key_column,
                          dry_run=dry_run, model=model)
        _attach_hint(out, sess, layer)
        return out
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_READ_ONLY)
def inspect(
    op: Literal["layers", "rows", "batch", "at"],
    layer: str | None = None,
    n: int = 10,
    include_geometry: bool = False,
    include_rowid: bool = False,
    offset: int = 0,
    where: str | None = None,
    columns: list[str] | None = None,
    batch_size: int = 200,
    cursor: str | None = None,
    points: list[dict] | None = None,
    radius_m: float = 100.0,
    layers: list[str] | None = None,
    per_layer_limit: int = 3,
    ctx: Context | None = None,
) -> dict:
    """Explore the session — inventory, row samples, cursor-paginated
    reads, and spatial "what's here" lookups.

    - `op="layers"`: inventory of every session layer + checkpoint state.
    - `op="rows"` (layer, n?, include_geometry?, include_rowid?,
      offset?, where?): sample rows from `layer`. Returns both a
      rendered markdown table (`table_md`) and a structured `rows`
      list (`[{col: val, ...}, ...]`) so programmatic pipelines
      don't have to parse the markdown. Caps: 200 rows without
      geometry, 10 with. Set `include_rowid=True` to prepend a
      `rowid` column — useful when you plan to `write_attributes(layer,
      key_column="rowid", values={…})` against the sampled rows
      without switching to `op="batch"`.
    - `op="batch"` (layer or cursor, columns?, where?, batch_size?):
      cursor-paginated reader for large layers. First call: pass
      `layer`. Subsequent calls: pass `cursor` from the previous
      response. Each row carries `rowid` automatically.
    - `op="at"` (points=[{id?, x_3011, y_3011}], radius_m?, layers?,
      columns?, per_layer_limit?): "what's near each of these
      points?" across session layers. Up to 500 points per call.
      `per_layer_limit` caps features per layer per point (default 3).
    """
    try:
        sess = _session(ctx)
    except SessionExpired as e:
        return _error_response(e)

    if op == "layers":
        try:
            return op_list_layers(sess)
        except Exception as e:
            return _error_response(e)

    if op == "batch":
        if cursor is None and not layer:
            return _server_error(
                "missing_arg",
                "inspect op='batch' requires `layer` on the first call or `cursor` to continue.",
            )
        try:
            return op_batch_iterate(
                sess, layer or "",
                columns=columns, batch_size=batch_size,
                cursor=cursor, where=where,
            )
        except Exception as e:
            return _error_response(e)

    if op == "at":
        if not points:
            return _server_error("missing_arg", "inspect op='at' requires `points`.")
        if len(points) > 500:
            return _server_error(
                "too_many_points",
                f"got {len(points)}, cap is 500. Batch into smaller calls.",
            )
        try:
            return op_inspect_locations(
                sess, points,
                radius_m=radius_m, layers=layers,
                columns=columns, per_layer_limit=per_layer_limit,
            )
        except Exception as e:
            return _error_response(e)

    if op == "rows":
        if not layer:
            return _server_error("missing_arg", "inspect op='rows' requires `layer`.")
        meta = sess.layers.get(layer)
        if meta is None:
            return _server_error(
                "unknown_layer",
                f"unknown layer '{layer}' in this session.",
                available=list(sess.layers),
            )
        cap = 10 if include_geometry else 200
        n_rows = max(1, min(int(n), cap))
        geom_col = meta.attributes.get("__geom_col__") or ""
        cols = [c for c in meta.attributes if not c.startswith("__")]
        select_cols = []
        if include_rowid:
            select_cols.append("rowid")
        for c in cols:
            if c == geom_col:
                if include_geometry:
                    select_cols.append(f'ST_AsText({_qi(c)}) AS geom_wkt')
            else:
                select_cols.append(_qi(c))
        where_sql = ""
        if where:
            from .operations import _assert_predicate as _p, OpError as _OE
            try:
                _p(where, "where")
            except _OE as e:
                return _server_error("op_failed", str(e))
            where_sql = f" WHERE {where}"
        sql = (
            f"SELECT {', '.join(select_cols)} FROM {_qi(layer)}"
            f"{where_sql} LIMIT {n_rows} OFFSET {int(offset)}"
        )
        try:
            rows = sess.conn.execute(sql).fetchall()
            col_names = [d[0] for d in sess.conn.description]
        except Exception as e:
            return _server_error(type(e).__name__, str(e))
        effective_total = meta.feature_count
        if where:
            try:
                cnt = sess.conn.execute(
                    f"SELECT COUNT(*) FROM {_qi(layer)} WHERE {where}"
                ).fetchone()
                effective_total = int(cnt[0]) if cnt else effective_total
            except Exception:
                pass
        if not rows:
            return {
                "layer": layer, "rows_shown": 0,
                "rows_total": effective_total, "cap": cap,
                "rows": [],
                "table_md": f"(no rows; {meta.feature_count} in layer)",
            }
        md = ["| " + " | ".join(col_names) + " |",
              "|" + "|".join(["---"] * len(col_names)) + "|"]
        for r in rows:
            md.append("| " + " | ".join(_md_cell(v) for v in r) + " |")
        header = f"Showing {len(rows)} of {effective_total}"
        if where:
            header += f" (filtered; layer has {meta.feature_count})"
        header += f" rows in `{layer}`."
        parts = [header, "", "\n".join(md)]
        seen = int(offset) + len(rows)
        remaining = max(0, effective_total - seen)
        if remaining > 0:
            parts.append(
                f"\n_{remaining} more row{'s' if remaining != 1 else ''} "
                f"not shown; raise `n` (cap {cap}) or `offset` to see them._"
            )
        if include_geometry:
            parts.append("\n_geom_wkt is large — request only when needed._")
        # Structured `rows` list alongside the markdown, so programmatic
        # pipelines (sample → annotate) don't have to parse the markdown
        # back out. The markdown is still the primary display surface.
        rows_list = [dict(zip(col_names, r)) for r in rows]
        return {
            "layer": layer,
            "rows_shown": len(rows),
            "rows_total": effective_total,
            "cap": cap,
            "rows": rows_list,
            "table_md": "\n".join(parts),
        }

    return _server_error("unsupported_operation",
                         f"op={op!r} not supported",
                         supported=["layers", "rows", "batch", "at"])


@mcp.tool(annotations=_SAFE_MUTATION)
@_audited("layer")
def layer(
    op: Literal["show", "hide", "rename", "drop", "set_notes"],
    name: str | None = None,
    new_name: str | None = None,
    notes: str | None = None,
    layers: list[str] | None = None,
    title: str | None = None,
    style: dict | None = None,
    description: str = "",
    ctx: Context | None = None,
) -> dict:
    """Layer visibility + lifecycle.

    - `op="show"` (layers, title?, style?): REPLACE the viewer's
      visible set with `layers`. Pass `title=None` to preserve the
      existing panel title; `""` to clear it. `style` is a per-layer
      styling spec (see below).
    - `op="hide"` (layers?): remove `layers` from the visible set.
      With no `layers`, hides all.
    - `op="rename"` (name, new_name): rename a layer. Reversible in a
      covering checkpoint.
    - `op="drop"` (name): remove a layer. Reversible in a covering
      checkpoint (full layer snapshotted).
    - `op="set_notes"` (name, notes): attach free-text narration to a
      layer. Shown in `inspect(op="layers")` and `sources(layer)`.

    Style spec (op="show"):

        style = {
          "<layer>": {
            "column": "<attr>", "scale": "categorical" | "linear",
            "palette": {"v": "#rrggbb", ...} | ["#lo", "#hi"] | None,
            "size":    {"column": "<attr>", "range": [lo, hi]},
            "opacity": {"column": "<attr>", "range": [lo, hi]},
            "stroke":  {"column": "<attr>", "range": [lo, hi]},
          }
        }
    """
    try:
        sess = _session(ctx)
    except SessionExpired as e:
        return _error_response(e)

    if op == "show":
        return _do_show(sess, layers or [], title=title, style=style)

    if op == "hide":
        try:
            out = op_hide_layers(sess, layers)
            out["viewer_url"] = _abs_url(f"/view/{sess.id}")
            return out
        except Exception as e:
            return _error_response(e)

    if op == "rename":
        if not name or not new_name:
            return _server_error("missing_arg",
                                 "layer op='rename' requires `name` and `new_name`.")
        try:
            out = op_rename_layer(sess, name, new_name)
            _attach_hint(out, sess, new_name)
            return out
        except Exception as e:
            return _error_response(e)

    if op == "drop":
        if not name:
            return _server_error("missing_arg", "layer op='drop' requires `name`.")
        try:
            out = op_drop_layer(sess, name)
            _attach_hint(out, sess, name)
            return out
        except Exception as e:
            return _error_response(e)

    if op == "set_notes":
        if not name or notes is None:
            return _server_error("missing_arg",
                                 "layer op='set_notes' requires `name` and `notes`.")
        try:
            return op_set_notes(sess, name, notes)
        except Exception as e:
            return _error_response(e)

    return _server_error(
        "unsupported_operation",
        f"op={op!r} not supported",
        supported=["show", "hide", "rename", "drop", "set_notes"],
    )


def _do_show(sess: Session, layers_in: list[str],
             title: str | None, style: dict | None) -> dict:
    missing = [n for n in layers_in if n not in sess.layers]
    sess.visible_layers = [n for n in layers_in if n in sess.layers]
    if title is not None:
        sess.visible_title = decode_unicode_escapes(title) or None

    VALID_SCALES = {"categorical", "linear"}
    CHANNEL_KEYS = ("size", "opacity", "stroke")
    if style:
        for lname, spec in style.items():
            if lname not in sess.layers:
                continue
            spec = spec or {}
            scale = spec.get("scale")
            if scale is not None and scale not in VALID_SCALES:
                return _server_error(
                    "invalid_style",
                    f"style[{lname!r}].scale must be one of {sorted(VALID_SCALES)}, "
                    f"got {scale!r}",
                )
            for ch in CHANNEL_KEYS:
                if ch not in spec:
                    continue
                ch_spec = spec[ch]
                if ch_spec is None:
                    continue
                if not isinstance(ch_spec, dict):
                    return _server_error(
                        "invalid_style",
                        f"style[{lname!r}].{ch} must be a dict like "
                        f"{{'column': '...', 'range': [lo, hi]}}, "
                        f"got {type(ch_spec).__name__}",
                    )
                if not ch_spec.get("column"):
                    return _server_error(
                        "invalid_style",
                        f"style[{lname!r}].{ch} missing required 'column'",
                    )
                rng = ch_spec.get("range")
                if (not isinstance(rng, list) or len(rng) != 2
                    or not all(isinstance(v, (int, float)) for v in rng)):
                    return _server_error(
                        "invalid_style",
                        f"style[{lname!r}].{ch}.range must be [lo, hi] numbers, got {rng!r}",
                    )
            sess.visible_styles[lname] = spec

    sess.visible_styles = {k: v for k, v in sess.visible_styles.items()
                           if k in sess.visible_layers}
    sess.bump_version()
    compact = []
    for n in sess.visible_layers:
        m = sess.layers[n]
        compact.append({
            "name": n,
            "feature_count": m.feature_count,
            "geometry_type": m.geometry_type,
            "style_applied": bool(sess.visible_styles.get(n)),
        })
    return {
        "title": sess.visible_title,
        "viewer_url": _abs_url(f"/view/{sess.id}"),
        "visible_layers": compact,
        "unknown_layers": missing,
        "styles": sess.visible_styles,
    }


@mcp.tool(annotations=_SAFE_MUTATION)
@_audited("export")
def export(
    layers: str | list[str],
    format: Literal["gpkg", "geojson", "csv", "parquet"] = "gpkg",
    cite: bool = False,
    merge_geojson: bool = False,
    description: str = "",
    ctx: Context | None = None,
) -> dict:
    """Export one or many session layers to a downloadable data artefact.
    Returns URL(s) valid for 24 h. For PNG map images use `render_map`
    — this tool is data-only.

    Formats:
      - **gpkg** (default): OGC GeoPackage in native EPSG:3011.
        Single-layer → one .gpkg file. Multi-layer → one .gpkg with
        every layer inside (QGIS-friendly).
      - **geojson**: EPSG:4326 FeatureCollection. Multi-layer emits
        one file per layer by default; pass `merge_geojson=True` for
        a single FeatureCollection with a `_layer` property.
      - **csv**: attribute columns + geometry as WKT.
      - **parquet**: columnar, zstd-compressed, geometry as WKB.

    Args:
        layers: Single layer name or list.
        format: Output format (default 'gpkg').
        cite: If True, include a provenance markdown block alongside
              the URL(s) (folds the old `export_and_cite` pattern).
        merge_geojson: For geojson multi-layer, emit one combined file.
    """
    try:
        sess = _session(ctx)
    except SessionExpired as e:
        return _error_response(e)
    layers_list = [layers] if isinstance(layers, str) else list(layers)

    try:
        if len(layers_list) == 1:
            if cite:
                out = op_export_and_cite(sess, layers_list[0], fmt=format)
            else:
                out = op_export(sess, layers_list[0], fmt=format)
        else:
            out = op_export_layers(sess, layers_list, fmt=format,
                                   merge_geojson=merge_geojson)
            if cite:
                # Bundle provenance markdown for the combined export.
                cite_md = op_sources(sess, None)
                out["citation_md"] = cite_md
        if "url" in out:
            out["url"] = _abs_url(out["url"])
        for f in out.get("files", []):
            if "url" in f:
                f["url"] = _abs_url(f["url"])
        return out
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_SAFE_MUTATION)
@_audited("render_map")
def render_map(
    layers: list[str],
    title: str | None = None,
    legend: bool = True,
    width_px: int = 1600,
    height_px: int = 1000,
    description: str = "",
    ctx: Context | None = None,
) -> dict:
    """Render the given layers to a styled PNG map and return a download URL.

    Reads the per-layer style set by `layer(op="show", style=...)` —
    call that first if you want themed colours, size / opacity / stroke
    channels, or a categorical palette. Layers with no style fall back
    to a default solid colour.

    Underlay: Carto Positron tiled basemap read from a pre-warmed
    local cache (no runtime network access; see `docs/rendering.md`).
    If the cache is missing the renderer falls back to a paper-toned
    backdrop with a faint cartographer's grid. Vector overlay always
    renders regardless.

    Args:
        layers: names of session layers to render.
        title: optional figure title; falls back to the session's
               current `layer(op="show")` title if set.
        legend: include a per-layer legend (default True).
        width_px, height_px: output dimensions. Defaults 1600×1000.

    Returns: `{url, format="png", width, height, bbox_3011,
    size_bytes, expires_in_s, hint}`. URL is absolute when
    `GEODATA_PUBLIC_URL` is set; relative otherwise. Expires after 24 h.
    """
    try:
        sess = _session(ctx)
    except SessionExpired as e:
        return _error_response(e)
    if not layers:
        return _server_error("missing_arg",
                             "render_map requires at least one layer.")
    try:
        from . import render as _render
        import secrets
        token = secrets.token_urlsafe(12)
        out_dir = EXPORT_ROOT / token
        info = _render.render_map_png(
            sess, layers, out_dir,
            title=title, legend=legend,
            width_px=int(width_px), height_px=int(height_px),
        )
        size = (out_dir / info["filename"]).stat().st_size
        url = _abs_url(f"/exports/{token}/{info['filename']}")
        return {
            "url": url, "format": "png",
            "width": info["width"], "height": info["height"],
            "bbox_3011": info["bbox_3011"],
            "size_bytes": size,
            "expires_in_s": EXPORT_TTL_S,
            "hint": ("PNG artefact rendered server-side. Carto Positron "
                     "tiled basemap underlay (pre-warmed local cache, "
                     "no runtime egress) plus desaturated vector overlay. "
                     "Embed directly in docs, slides, or messages."),
        }
    except Exception as e:
        return _error_response(e)


@mcp.tool(annotations=_READ_ONLY)
def sources(layer: str | None = None, ctx: Context | None = None) -> str:
    """Structured provenance report (publisher, licence, URL,
    retrieval date, operations applied) for a layer or all session
    layers, as markdown. Use this to cite where data came from after
    a multi-step analysis.
    """
    return op_sources(_session(ctx), layer)


@mcp.tool(annotations=_SAFE_MUTATION)
@_audited("checkpoint")
def checkpoint(
    op: Literal["create", "rollback", "commit"],
    name: str,
    layers: list[str] | None = None,
    description: str = "",
    ctx: Context | None = None,
) -> dict:
    """Named savepoints that make in-place mutations reversible.

    - `op="create"` (name, layers?): snapshot mutations going forward.
      Pass `layers=[...]` to scope to specific layers; omit for
      whole-session coverage. Multiple checkpoints can be active at
      once (column-scoped snapshots, not full-layer copies).
    - `op="rollback"` (name): restore all covered mutations; discard
      the checkpoint and its snapshots.
    - `op="commit"` (name): make covered mutations permanent; discard
      snapshots, reclaim storage.

    Covered mutations: `edit_field` (add/update/drop/classify),
    `write_attributes`, and `layer` (rename/drop).
    """
    try:
        sess = _session(ctx)
        if op == "create":
            return op_checkpoint(sess, name, layers=layers)
        if op == "rollback":
            return op_rollback(sess, name)
        if op == "commit":
            return op_commit(sess, name)
        return _server_error(
            "unsupported_operation",
            f"op={op!r} not supported",
            supported=["create", "rollback", "commit"],
        )
    except Exception as e:
        return _error_response(e)


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="Serve over HTTP/SSE")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    REGISTRY.start_gc()

    if args.http:
        import uvicorn
        from .http_app import build_http_app
        app = build_http_app(
            mcp=mcp, root=ROOT,
            layer_summary=_layer_summary, qi=_qi,
        )
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
