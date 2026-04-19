# MCP tool reference

**35 tools** across four categories + a macros tier. Endpoint:
`https://geo.benjaminhenriksson.com/mcp` (OAuth 2.1 + PKCE via invite code
for claude.ai; legacy shared bearer for Claude Code CLI). Every tool
carries MCP `toolAnnotations` (`readOnlyHint`, `destructiveHint=false`)
so clients can auto-approve the safe ones without per-call prompts.

All spatial tools operate in **EPSG:3011** (SWEREF 99 18 00). Coordinates in
tool arguments and return values use that CRS unless otherwise noted. The
viewer reprojects to EPSG:4326 at the `/api/.../geojson` boundary. The
server's `instructions` string (~9 KB workflow primer) is delivered to the
MCP client at connection time — it covers CRS conventions, canonical SCB
join keys, the bulk-enrichment loop, macro-tool preferences, and when the
LLM should push back instead of ploughing on.

## Tool index

**Discovery / read-only** (`readOnlyHint=true`): `search_data`,
`describe_dataset`, `geocode`, `inspect`, `stats`, `sources`, `list_layers`,
`batch_iterate`, `inspect_location`, `inspect_locations`.

**Layer creation / session state** (`destructiveHint=false`): `load`,
`load_many`, `filter`, `spatial`, `execute_sql`, `create_layer`, `export`,
`show`, `hide`, `set_notes`.

**In-place layer mutation** (`destructiveHint=false`, reversible inside a
checkpoint): `add_field`, `update_field`, `drop_field`, `annotate`,
`drop_layer`, `rename_layer`.

**Transaction control**: `checkpoint`, `rollback`, `commit`. Checkpoints
accept an optional `layers=[...]` scope, and multiple can be active
simultaneously on overlapping or disjoint scopes.

---

## 1. `search_data(query: str = "")`

Fuzzy-search the catalog of locally available datasets. `rapidfuzz.WRatio` with
lowercase normalization over name + description + keywords + attribute
descriptions in Swedish and English.

Returns structured metadata per match: id, name (SV/EN), description, source
type, geometry type, feature count, coverage, temporal range, CRS, attributes
(name/type/description/sample values), publisher, license, match score.

---

## 2. `geocode(name: str, limit: int = 5, all_kinds: bool = False)`

Resolve a Swedish place name or `street number` against SBK label layers.

Two modes, tried in order:
- **Composite address** (triggered when the input matches `<street> <number>`):
  spatial-pairs `NamnText_point[GRUPP='Gatunamn']` with `AdressText_point` where
  `TEXT == number` within 250 m of the street label. Returns at most one match
  per (street, number) pair.
- **Place name**: `jaro_winkler_similarity` across `NamnText_point` filtered to
  useful GRUPP values (Stadsdel, Distrikt, Kvarter, Gatunamn, Bostadsbyggnad,
  Samhällsfunktionsbyggnad, Idrottsanläggning, Koloniområde, Sjö, Vattendrag,
  Natur, Trafikplats, Bytesplats, Markanläggning, Övrig anläggning).

For Stadsdel / Distrikt / Stadsdelsnämndsområde / Kvarter matches, the returned
`bbox_3011` is the extent of the containing `Adm_area` polygon — not a 200 m
label-point radius. Other matches get a 200 m box.

Returns `{query, coverage, matches: [{name, kind, subkind, x_3011, y_3011, bbox_3011, score}]}`.

---

## 3. `load(dataset_id, bbox_3011?, limit?, layer_name?, where?, intersect_layer?)`

Read a catalog dataset into the session as a DuckDB table, optionally filtered
at load time. Filters AND-combine before the 100,000-feature cap.

- `bbox_3011`: `[xmin, ymin, xmax, ymax]` rectangular filter.
- `where`: SQL WHERE against dataset columns (no `;`).
- `intersect_layer`: Name of an already-loaded session layer; features kept
  where they intersect the union of that layer's geometries. Preferred over
  hand-crafted bboxes for irregular polygons.

Provenance: `SourceRef` from the catalog entry; if `intersect_layer` is used,
the intersecting layer's sources are merged in.

Returns a layer summary (name, feature count, bbox, geometry type, attributes, provenance).

---

## 4. `filter(layer, where, result_name?)`

SQL WHERE over an existing session layer. Creates a new layer. Rejects `;`.
Provenance inherited from `layer`.

The WHERE expression accepts any DuckDB-compatible predicate, including
**spatial predicates on the `geom` column**:
- Attribute: `KATEGORI = 'Flerbostadshus'`
- Spatial: `ST_Contains(geom, ST_Point(153700, 6578000))`
- Distance: `ST_DWithin(geom, ST_Point(x, y), 200)`
- Mixed: `KATEGORI='Flerbostadshus' AND ST_Contains(geom, <polygon>)`

For "features of A that relate to any feature of B" use
`spatial(operation='select_by_location', ...)` instead.

---

## 5. `spatial(operation, ...)`

Spatial ops producing new layers. The `operation` parameter uses a JSON-Schema
enum so clients get autocomplete. Required argument set depends on `operation`:

| operation | required args | behavior |
|---|---|---|
| `select_by_location` | `layer`, `by_layer`, `predicate` | **Spatial WHERE**: rows of `layer` kept unchanged where their geom relates to ANY feature in `by_layer` by `predicate`. Predicates: `intersects` (default), `within`, `contains`, `dwithin` (+ `distance_m`). This is what you usually want for "buildings in district" / "DeSO containing point". |
| `clip` | `layer`, `by_layer` | `ST_Intersection(layer.geom, ST_Union_Agg(by_layer.geom))`. Geometries are **modified** — trimmed to the clipping shape. Keeps `layer`'s attributes. |
| `intersect` | `a_layer`, `b_layer` | **Geometric overlay** — one row per intersecting pair, `a.*` / `b.*` prefixed `a_` / `b_`, geometry = `ST_Intersection(a, b)`. Typically changes geometry kind (polygon × point → point). For a spatial join that keeps A's geometry, use `select_by_location` instead. |
| `buffer` | `layer`, `distance_m` | `ST_Buffer(geom, distance_m)` — metres in EPSG:3011. |
| `centroid` | `layer` | Per-feature `ST_Centroid`. |
| `dissolve` | `layer`, `by_columns?` | `ST_Union_Agg(geom)` grouped by columns (or all). Adds `feature_count`. |
| `convex_hull` | `layer`, `aggregate?` | Per-feature hull, or single aggregate hull with `aggregate=true`. |

All provenance is merged from parents, deduped by `dataset_id`. The returned
`geometry_type` is probed from the actual materialized rows — not inherited
from a parent — so you can tell when an op changed the geometry kind.

---

## 6. `stats(layer, columns?, group_by?, limit=100)`

Return a markdown aggregation table.

- Numeric columns → count / min / avg / max + non-null count.
- String columns → count / distinct count.
- Default `columns`: all numeric columns in the layer.
- `group_by`: list of attribute columns to group by. Rows sorted by `n` DESC.

Does not create a layer.

---

## 7. `execute_sql(sql, description="", result_name?, geometry_column?)`

Read-only SQL escape hatch.

**Validation** (raises `sql_rejected`):
- Parsed with `sqlglot` in DuckDB dialect.
- Exactly one statement.
- Statement must be `SELECT` / `WITH` / `UNION` / `Subquery`.
- Rejects `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `DROP`, `ALTER`, `MERGE`, `COPY` anywhere in the tree.

**Layer-vs-table decision** (in order of precedence):
1. If `geometry_column` is passed, layer mode is forced; the named column is cast to `GEOMETRY`.
2. Otherwise the result schema is probed via `DESCRIBE (<sql>)` and any column typed `GEOMETRY*` triggers layer mode.
3. Otherwise table mode returns up to 50 rows as markdown.
4. If a BLOB column happens to have a geometry-ish name (`geom`, `geometry`, `shape`, …) in table mode, the response includes a `geometry_hint` telling you to re-run with `geometry_column=…` or explicit `::GEOMETRY` cast.

**Execution** (raises `sql_failed` on runtime error):
- 30-s wall-clock timeout via `threading.Timer` calling `conn.interrupt()`.

Session layers appear as regular tables. DuckDB spatial functions are loaded.

Returns one of:
- `{mode: "layer", layer_name, feature_count, geometry_type, description}`
- `{mode: "table", rows, capped_at, description, markdown, geometry_hint?}`
- `{error: "sql_rejected"|"sql_failed", detail}`

---

## 8. `sources(layer?)`

Markdown report of provenance + operation chain. If `layer` is given, only that
layer; otherwise all session layers.

Per layer: feature count, creator tool, parent layers, operations applied
(walking ancestors via `parent_layers`), deduped source references with
publisher, license, URL, file path, retrieval date, and the `llm_sourced` flag
for data injected by a future `create_layer` tool.

---

## 9. `create_layer(name, data, source?, geometry_column?, crs="EPSG:4326")`

Inject LLM-provided data as a new session layer — for lookup tables the LLM
brings from external knowledge (a scrape, a manual curation, a published
report) that it wants to join with catalog layers.

- `data`: up to 1,000 rows as a list of dicts with identical keys.
- `source`: short free-text description of where the data came from. Stored as
  `llm_source_description` and always marked `llm_sourced=True` in provenance.
- `geometry_column` + `crs`: if one column carries WKT strings, it's parsed and
  reprojected to EPSG:3011. Default input CRS is WGS84 lng/lat (EPSG:4326).

Materialized via pyarrow → DuckDB. Same tool interface as catalog layers after
creation — filter/spatial/stats/execute_sql/export all work on it.

---

## 10. `export(layer, format="geojson")`

Write a session layer to a downloadable file and return its URL.

| format | extension | notes |
|---|---|---|
| `geojson` | `.geojson` | FeatureCollection reprojected to EPSG:4326 |
| `gpkg` | `.gpkg` | OGC GeoPackage native EPSG:3011 |
| `csv` | `.csv` | Attribute columns + geometry serialized as WKT |
| `parquet` | `.parquet` | Columnar, Zstd-compressed, geometry as WKB |

URL format: `/exports/<random-token>/<layer>.<ext>`. Token is a 16-byte
URL-safe secret, TTL 24 h, files auto-purged on subsequent export calls.
Returns size, expiry, and the layer's provenance for citation.

---

## 11. `inspect(layer, n=3, include_geometry=false, offset=0, where?)`

Raw row sampler. Markdown table. Hard caps: **25** rows without geometry, **10**
with geometry (`geom_wkt` column). `where` optional SQL predicate, no `;`.

---

## 12. `show(layers, title?)`

Mark layers visible in the viewer; return their summaries and the viewer URL.
Viewer pulls `/api/{session_id}/layer/{name}/geojson` per visible layer and
auto-styles by geometry type.

---

## Error envelope

All tools that can fail return a structured error object (not an exception):

```json
{"error": "<short_code>", "detail": "<human-readable>"}
```

Observed codes:
- `unknown_dataset`, `load_failed`, `filter_failed`
- `spatial_failed`, `missing_arg`, `unsupported_operation`
- `sql_rejected`, `sql_failed`
- `create_layer_failed`, `export_failed`

Never a Python traceback. Never implementation hints beyond the immediate cause.

---

## What changed in Phase 2 vs Phase 1

- **Tools**: +5 (`filter`, `spatial`, `stats`, `execute_sql`, `sources`). Phase 1 had `search_data`, `geocode`, `load`, `inspect`, `show`.
- **`load()`**: new `where` and `intersect_layer` params. Feature cap raised 50k → 100k.
- **`geocode()`**: composite street+number via spatial pairing; polygon-backed bbox for admin-kind matches.
- **Provenance**: now propagates through every derivation. `parent_layers` + `provenance` union deduped by `dataset_id`.

## What changed in Phase 3 vs Phase 2

- **Tools**: +2 (`create_layer`, `export`). Total 12.
- **New catalog datasets**: `deso_historical_changes` (SCB's official DeSO 2018→2025 mapping, 1,234 rows) and `deso_regso_mapping` (DeSO→RegSO for 6,160 rows).
- **`load()` for parquet**: cap raised from 100 K to 10 M — parquet is columnar and compact so the GPKG cap is inappropriate.
- **DeSO geom column**: renamed `sp_geometry` → `geom` at normalize time, so every spatial layer in the catalog uses `geom` consistently.
- **SCB dedup**: normalize step collapses the shadow-NULL duplicate rows that SCB publishes.
- **Audits**: `catalog_audit.py` + `cross_ref_audit.py` run post-normalize and fail the pipeline on real errors.

---

## What changed in Phase 4 — LLM-native workflow

- **Tools**: +14 (total **26**). New categories: in-place field ops,
  transaction control, AI-native iteration.
- **MCP annotations** on every tool (`readOnlyHint` / `destructiveHint`) so
  clients can auto-approve safe calls without per-action prompts.
- **Server `instructions`** — multi-paragraph system prompt delivered with the
  tool list at connection time. Covers workflow patterns, the CRS convention,
  SCB privacy-suppression and DeSO-2018→2025 footguns, and the enrichment
  loop (batch_iterate → annotate).
- **`inspect` cap raised** from 25 → 200 (attributes only); the 10 cap with
  geometry is unchanged.

### 13. `list_layers()`

Clean inventory: each layer's name, feature_count, geometry_type, bbox,
columns, creator, parent_layers, notes, visibility flag. Plus
`active_checkpoint` and `open_checkpoints`. Use this for orientation rather
than `sources()` when you don't need provenance detail.

### 14. `load_many(dataset_ids: list[str], bbox_3011?, limit?)`

Bulk variant of `load`. Returns `{loaded: [summaries], errors: [per-dataset]}`.
Single-call convenience when the LLM knows up front that it wants several
related datasets — shared bbox/limit only. Use individual `load` calls when
you need per-dataset arguments.

### 15. `add_field(layer, name, expr, field_type?)`

QGIS / ArcGIS Field Calculator. Add a column whose values are a DuckDB SQL
scalar expression per row. Type auto-inferred unless `field_type` is supplied
(`VARCHAR`, `DOUBLE`, `BIGINT`, `BOOLEAN`, `DATE`, `TIMESTAMP`). May reference
other columns of the same layer or scalar subqueries against other session
layers (useful for spatial-joined values).

In-place. Reversible inside an active `checkpoint(...)`.

### 16. `update_field(layer, name, expr, where?)`

Overwrite an existing column. Optional WHERE restricts which rows are
updated. In-place; reversible inside a checkpoint.

### 17. `drop_field(layer, name)`

Remove a column. Refuses to drop the geometry column (use `drop_layer` for
that). In-place; reversible inside a checkpoint.

### 18. `annotate(layer, values: dict, key_column="rowid")`

Attach LLM-classified per-feature attributes in one call. Payload:

```python
values = {
    rowid1: {"era": "functionalist", "confidence": 0.9, "note": "..."},
    rowid2: {"era": "art-nouveau",    "confidence": 0.7},
    ...
}
```

New columns are created on the fly with type inferred from the values
(`int`-only → BIGINT; mixed int/float → DOUBLE; bool → BOOLEAN; else
VARCHAR). Cap: 10,000 keys per call. Pair with `batch_iterate` for larger
layers. Reversible inside a checkpoint — snapshotted once per column
regardless of how many rows are touched.

### 19. `batch_iterate(layer, columns?, batch_size=200, cursor?, where?)`

Cursor-paginated read for layers too large to inspect in one go. First call
passes `layer` (and optionally `columns`, `where`, `batch_size`), response
carries `rows`, `next_cursor`, `exhausted`. Subsequent calls pass
`cursor=<next>`. Finish when `next_cursor` is null. Every batch includes a
`rowid` column suitable for `annotate(..., key_column="rowid")`. Max 500
rows per batch.

### 20. `inspect_location(x_3011, y_3011, radius_m=100, layers?, per_layer_limit=5)`

One-shot "what's here?" across many layers. For each session layer with
geometry (or the subset named in `layers`), returns up to `per_layer_limit`
features within `radius_m` of the point, sorted by distance, each annotated
with `distance_m`.

Natural conversational pattern: "what's at Sergels torg?" becomes one call
instead of a chained geocode → spatial(select_by_location) → inspect per
layer.

### 21. `drop_layer(name)`

Remove a layer from the session. Reversible inside a checkpoint (full layer
snapshotted) — otherwise irreversible.

### 22. `rename_layer(old, new)`

Rename. Reversible inside a checkpoint.

### 23. `set_notes(layer, notes)`

Attach free-text notes to a layer. Surfaces in `list_layers` and `sources`.
For narrating *why* a layer exists — "filtered to pre-1940 stone buildings
as a proxy for the historical core" — so the reasoning is recoverable from
the session state alone.

### 24. `checkpoint(name)`

Create a named checkpoint. Subsequent in-place mutations (`add_field`,
`update_field`, `drop_field`, `annotate`, `drop_layer`, `rename_layer`)
snapshot their pre-image column-scoped in a hidden side table. Storage cost
scales with the *diff*, not the full layer. One checkpoint active at a time;
nested checkpoints are not supported.

### 25. `rollback(name)`

Undo every in-place mutation made since `checkpoint(name)`. Snapshots are
applied in reverse order then discarded.

### 26. `commit(name)`

Make all mutations since `checkpoint(name)` permanent. Discards the snapshot
tables and frees the marker. The next mutation requires a fresh `checkpoint`
to be reversible.

---

## Canonical AI-native workflow

```python
# Setup
load_many(["sbk_buildings", "deso_2025"])
checkpoint("classify_era")

# Iteration loop
out = batch_iterate("sbk_buildings", columns=["objectid", "byggar", "name"], batch_size=200)
while True:
    # LLM reasons about the batch → produces {rowid: {"era": ..., "confidence": ...}}
    tags = classify_in_head(out["rows"])
    annotate("sbk_buildings", values=tags)
    if out["exhausted"]: break
    out = batch_iterate(cursor=out["next_cursor"])

# Verify, possibly iterate
stats("sbk_buildings", columns=["era"], group_by=["era"])
# if unhappy:
#     rollback("classify_era")
# else:
set_notes("sbk_buildings", "era classified by LLM on 2026-04-19")
commit("classify_era")
export("sbk_buildings", format="gpkg")
```

## Response hints

Most mutation tools include a `hint` field in their response flagging
checkpoint state — e.g. *"Mutation is reversible — call rollback('classify')
to undo."* or *"No checkpoint active — this mutation is not reversible."*.
Intended as teaching moments for LLMs just connecting.

---

## What changed in the post-feedback pass

Structured response to the user-testing session friction points:

**Tools added (+3 → 29 total):**
- `describe_dataset(id)` — full attribute schema for a single dataset. Paired
  with a `verbose=False` default on `search_data` to keep context small.
- `inspect_locations(points, ...)` — batch variant of `inspect_location`. Up
  to 500 points per call.
- `hide(layers)` — inverse of `show`. `layers=None` hides all.

**Response shape changes:**
- `execute_sql` now returns `truncated: true` + a `warning` when the result
  hits the 50-row cap, with a pointer to the `result_name=...` pagination path.
- `annotate` echoes `keys_cap` every time, and includes a `hint` about
  `create_layer` + `add_field` for >10 k workflows.
- `inspect_location` reports `unknown_layers` and emits a `hint` when the
  search radius turns up nothing or when the session has no geometric layers.
  `columns` parameter filters returned attributes. NULL attributes are
  elided to keep payload small.
- Mutation responses now report `covering_checkpoints: [...]` (list of
  checkpoints that would roll this mutation back), not just a single
  `active_checkpoint`.
- `batch_iterate` remembers the first-call `batch_size` across cursor calls,
  so continuation calls default to the intended size. Explicit `batch_size`
  on a cursor call still overrides.

**Scoped, concurrent checkpoints:**
- `checkpoint(name, layers=[...])` — scope to named layers. `layers=None`
  (default) covers every layer, keeping back-compat.
- Multiple checkpoints may be active at once. A mutation snapshots against
  every covering active checkpoint, so nested or parallel workflows don't
  couple.
- `rollback` / `commit` now surface `other_active` in their response.

**Public URL in responses:**
- `show` and `export` return absolute URLs when the server is deployed
  behind `GEODATA_PUBLIC_URL` (e.g.
  `https://geo.benjaminhenriksson.com/view/<id>` rather than `/view/<id>`).

**Viewer UX:**
- Auto-refresh: viewer polls `/api/<sid>/version` every 2 s and re-syncs on
  change — new layers appear, removed layers disappear, mutated layer data
  refreshes in-place, no manual reload.
- Popup precedence: click returns the feature from the *smallest-bbox*
  layer, not the topmost. Other layers at the same point are listed
  ("Also at this point: …") so you can drill into them if you want.
- Layers re-ordered on draw: small-bbox layers drawn on top of large ones,
  improving click-through and visual legibility.
- Layer notes shown in the legend row.

**SCB data fixes (re-normalized):**
- Every SCB parquet now has a unified `value` numeric column. The SCB
  convention of naming the value column after the first variable
  (e.g. "Andel av befolkningen i inkomstklass") is gone.
- Every SCB parquet carries `region_kind` ∈ {`deso`, `regso`, `kommun`,
  `country`, `other`}, plus `region_code` and `region_name`. Filter with
  `WHERE region_kind = 'deso'` instead of `LIKE '0180%'` hacks.
- Catalog entries regenerated for all 31 SCB tables. Audits clean.

**Instructions upgrade:**
- New "When the LLM should push back, not plough on" section — explicit
  guidance to surface warnings / hints / truncation and to name missing
  data rather than fabricate.
- "Bulk enrichment pattern" example showing the canonical batch_iterate →
  annotate loop.
- Stronger preference for `load_many` over repeated `load` calls.
- Pointer to `describe_dataset(id)` for attribute schemas.

---

## Macro tools (post-feedback pass)

Added to reduce round-trips under claude.ai's per-turn tool-call cap:

### `top_n(layer, by, n=10, ascending=False, result_name=None)`

filter + `ORDER BY` + `LIMIT` in one call. Produces a new layer with the
top (or bottom) `n` rows. `by` is a SQL ordering expression — can be a
column name, a function call, or a full expression. Provenance inherits
from `layer`.

### `baseline_stats(layer, expression, group_by=None)`

Descriptive statistics (count / mean / median / p25 / p75 / min / max /
stddev) for a SQL expression over a whole layer, optionally grouped.
Returns `{stats: {...}}` for ungrouped or `{groups: [...]}` with
group-by rows. Use for "compute city-wide median income as a baseline
before comparing a subset."

### `classify(layer, name, rules=[{when, then}], default=None)`

CASE-WHEN-THEN shorthand. Adds a new column whose value is the `then`
of the first matching rule. Reversible inside a covering checkpoint.
Example:

```
classify("deso", "income_band", rules=[
    {"when": "median < 300", "then": "low"},
    {"when": "median BETWEEN 300 AND 500", "then": "mid"},
    {"when": "median > 500", "then": "high"},
], default="unknown")
```

### `export_and_cite(layer, format='gpkg')`

`export(layer, format)` + `sources(layer)` in one call — URL + full
markdown citations. One round-trip for the last step of every
publishable workflow.

### `export_many(layers, format='gpkg', merge_geojson=False)`

Export several layers under one 24-h download token.

- `format='gpkg'` (recommended): **one multi-layer `.gpkg` file** — each
  input layer preserved as its own GPKG layer with its native geometry
  and attributes. Opens cleanly in QGIS/ArcGIS.
- `format='geojson'` + `merge_geojson=True`: single `.geojson` with a
  merged FeatureCollection; every feature gets `_layer: "<name>"` so
  downstream consumers can split back apart.
- `format='geojson'|'csv'|'parquet'` (merge_geojson=False, default for
  those): one file per layer under the same token dir. List of URLs
  returned.

HUGEINT columns (a common byproduct of DuckDB's `SUM(CAST(x AS BIGINT))`
auto-promotion) are auto-coerced to BIGINT at export so GeoJSON's
JSON-number serialization doesn't fail with the cryptic "precision up to
19" error.

---

## Canonical join keys across every SCB table

Every `scb_*` parquet now carries these columns in addition to the
tabular raw:

- `desokod` — DeSO code (9 chars), equals `region_code` when
  `region_kind='deso'`. Join to `deso_2025.desokod` / `deso_2018.desokod`.
- `desokod_2025` — 2025-grid equivalent of `desokod`. Bridges 2018 → 2025
  via `deso_historical_changes`. Equals `desokod` when the DeSO is
  unchanged since 2018.
- `regsokod`, `regso_name` — parent RegSO. Joined via `deso_regso_mapping`.
- `kommunkod`, `kommun_name` — parent kommun. Joined via
  `deso_regso_mapping`.

All six are NULL for non-DeSO rows (RegSO / kommun / country). Prefer
them over the raw `region` column for joins — they're uniform across
the 31 SCB tables.

---

## Richer default responses

`load` / `filter` / `spatial` / `create_layer` / `top_n` / `classify` /
`execute_sql` (layer mode) now return a `quick_stats` block and a 3-row
`sample` by default, in addition to the existing feature_count / bbox /
attributes / provenance. Skipped for layers above
`GEODATA_QUICK_STATS_CAP` (200k features by default). Saves the typical
"call stats() and inspect() after load" round-trip.

---

## `show` thematic styling

`show(layers, style={...})` accepts a per-layer style spec:

```
show(["buildings"], style={
    "buildings": {
        "column": "era",
        "scale": "categorical",
        "palette": {
            "pre-1900": "#6a3d9a",
            "functionalist": "#1f78b4",
            "post-war": "#33a02c",
            "modern": "#ff7f00",
        },
    },
})
```

Linear palettes: `"scale": "linear"` + `"palette": ["#lo", "#hi"]`.
Unstyled layers keep their default solid color. Styles persist in
session state and are consumed automatically by the viewer's 2 s
auto-refresh poll.
