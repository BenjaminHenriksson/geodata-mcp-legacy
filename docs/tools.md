# MCP tool reference

As of Phase 3. **Twelve tools**. Endpoint: `https://geo.benjaminhenriksson.com/mcp` (bearer auth).

All spatial tools operate in **EPSG:3011** (SWEREF 99 18 00). Coordinates in tool
arguments and return values use that CRS unless otherwise noted. The viewer
reprojects to EPSG:4326 at the `/api/.../geojson` boundary.

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
