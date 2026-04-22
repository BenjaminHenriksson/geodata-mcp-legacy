# MCP tool reference

**11 tools.** Endpoint: `https://geo.benjaminhenriksson.com/mcp`
(OAuth 2.1 + PKCE via invite code for web custom-connector flows
like claude.ai, ChatGPT, Gemini, and the rest; legacy shared bearer
for CLI / desktop clients that haven't shipped OAuth yet). Every
tool carries MCP `toolAnnotations` (`readOnlyHint`,
`destructiveHint=false`) so any MCP-capable client can auto-approve
the safe ones without per-call prompts.

All spatial tools operate in **EPSG:3011** (SWEREF 99 18 00).
Coordinates in tool arguments and return values use that CRS unless
otherwise noted. The viewer reprojects to EPSG:4326 at the
`/api/.../geojson` boundary. The server's `instructions` string
(~6 KB workflow primer) is delivered to the MCP client at
connection time. It covers the tool index, the canonical SCB join
keys, the bulk-enrichment loop, auditability, and when to push back
instead of ploughing on.

## Tool index

1. **`catalog`** — catalog search + describe (`readOnlyHint`).
2. **`geocode`** — name ↔ coords ↔ bbox (`readOnlyHint`).
3. **`load`** — pull catalog datasets or inject LLM-provided rows.
4. **`execute_sql`** — read-only DuckDB + Spatial SQL sandbox.
5. **`derive`** — new layer from existing (filter / spatial / top_n).
6. **`edit_field`** — add / update / drop / classify / annotate columns.
7. **`inspect`** — session inventory, rows, cursor pagination, spatial "what's here" (`readOnlyHint`).
8. **`layer`** — show / hide / rename / drop / set_notes.
9. **`export`** — single or many layers, gpkg / geojson / csv / parquet / png; optional citation bundle.
10. **`sources`** — provenance markdown (`readOnlyHint`).
11. **`checkpoint`** — create / rollback / commit savepoints.

Tools 1, 2, 7, 10 are read-only. The rest mutate session state (load
creates layers, edit_field mutates columns, etc.) but are non-destructive
(reversible if wrapped in a checkpoint where applicable).

---

## The `op` enum pattern

Five tools — `geocode`, `derive`, `edit_field`, `inspect`, `layer`,
`checkpoint` — take `op: Literal[...]` as the first required argument
to select among a small set of related sub-operations. This keeps the
11-tool surface focused on what the model is thinking about ("I want
a new layer derived from this one") rather than how it's
implemented ("did I call `filter` or `top_n` or `buffer`"). The
`Literal` values are visible directly in the tool schema, so the LLM
sees the valid ops at a glance.

`load`, `execute_sql`, and `export` also multiplex behaviour, but the
dispatch is on natural argument shape (which fields you pass, or
`format=...`) rather than a dedicated `op` enum — see each tool's
signature below.

---

## Auditability: `description` on every mutating tool

`load`, `execute_sql`, `derive`, `edit_field`, `layer`, `export`,
`checkpoint` accept a `description: str = ""` keyword argument. The
user sees this in their viewer's audit panel alongside the actual SQL
the tool generated, and can decide whether to trust the LLM's work
without reading code.

The decorator behind every mutating tool opens an audit context that:

- mints a per-call `correlation_id`,
- captures every SQL statement executed inside the tool body
  (including helper queries like `DESCRIBE`, bbox aggregates,
  `COUNT`s),
- records timing + status,
- attaches the `description` to the resulting `Operation` record.

Records mirror to `<sid>.audit.jsonl` (append-only) and serve at
`GET /api/<sid>/audit_log`. Operations called without a description
appear flagged "no description provided" — a visible nudge that the
LLM should be filling them in.

Treat the description like a commit message: short, specific, in the
user's frame ("filter to schools within 500 m of metro stops, per
user request"), not yours ("call ST_DWithin on layer foo"). Tool
signatures below omit `description` for readability, but it's
accepted everywhere mutations happen.

---

## 1. `catalog(query="", id=None, verbose=False, limit=20)`

Catalog search + describe. Pass `id` for full metadata of a single
dataset (attribute schema, sample values, descriptions); otherwise
fuzzy-search the 65-dataset catalog via `rapidfuzz.WRatio` over name
+ description + keywords + attribute descriptions (Swedish + English).

- Compact search (`verbose=False`, default): id / name / description /
  coverage / temporal / licence per hit. 20–50 KB → ~2 KB per hit.
- Verbose search (`verbose=True`): include full attribute schema for
  every hit (can be 20–50 KB total).
- Describe (`id="..."`): full metadata for one dataset. Prefer this
  over `verbose=True` once you know what you want.

Returns a list of dataset summaries with a `hint` field prompting the
compact-then-describe pattern.

---

## 2. `geocode(op, ...)` — name ↔ coords ↔ bbox

Resolve a Swedish place name or street+number against SBK label
layers. Three sub-ops.

### `op="forward"` — name → EPSG:3011 coords + bbox

Args: `name: str`, `limit: int = 5`, `all_kinds: bool = False`.

Two matching modes, tried in order:
- **Composite address** (triggered when input matches `<street> <number>`):
  spatial-pairs `NamnText_point[GRUPP='Gatunamn']` with
  `AdressText_point` where `TEXT == number` within 250 m of the
  street label. At most one match per (street, number).
- **Place name**: `jaro_winkler_similarity` across `NamnText_point`
  filtered to useful GRUPP values (Stadsdel, Distrikt, Kvarter,
  Gatunamn, Bostadsbyggnad, Samhällsfunktionsbyggnad,
  Idrottsanläggning, Koloniområde, Sjö, Vattendrag, Natur,
  Trafikplats, Bytesplats, Markanläggning, Övrig anläggning).

Stadsdel / Distrikt / Stadsdelsnämndsområde / Kvarter matches return
the extent of the containing `Adm_area` polygon as `bbox_3011`; other
matches get a 200 m label-point box.

Returns `{query, coverage, matches: [{name, kind, subkind, x_3011,
y_3011, bbox_3011, score}]}`. By default deduplicated across GRUPP
(one row per named thing). Pass `all_kinds=True` to see every label
variant.

### `op="reverse"` — coords → containing admin areas

Args: `x_3011: float`, `y_3011: float`.

**Always use this instead of guessing neighborhoods from raw
coordinates.** Returns the Stadsdel / Stadsdelsnämndsområde /
Distrikt / Kvarter / Kommun polygons that contain the point, plus a
`by_kategori` grouping for convenience. If the point is outside
SBK's coverage, `containing` is empty with a warning — state the
location is unidentifiable, do not invent.

### `op="bbox"` — name → EPSG:3011 bbox (optionally buffered)

Args: `name: str`, `buffer_m: float = 0.0`.

Thin convenience over `op="forward"`: takes the best match and
returns its `bbox_3011`, optionally expanded by `buffer_m` on every
side. Folds the "geocode → eyeball coords → build a bbox by hand"
pattern into one call.

Returns `{name, kind, subkind, score, center_3011, bbox_3011, buffer_m}`
or a `no_match` error.

---

## 3. `load(op, ...)` — catalog pull OR inline injection

Single entry point for getting data into the session.

### `op="catalog"` — pull 1..N catalog datasets

Args: `dataset_ids: list[str]` (required) + per-dataset filters (single-id only): `bbox_3011`, `where`, `intersect_layer`, `layer_name`. Also `limit: int | None`.

- Single id: all filters honoured. Server cap: 100,000 features.
- Multiple ids: `bbox_3011` and `limit` apply across the set;
  `where` / `intersect_layer` / `layer_name` are ignored (use
  separate single-id calls if you need per-dataset filtering).

`intersect_layer` is preferred over hand-crafted bboxes for irregular
polygons (e.g. clipping to a Stadsdel) — pass the name of an
already-loaded session layer and the dataset is filtered to features
intersecting the union of its geometries.

Provenance: `SourceRef` from each catalog entry; if `intersect_layer`
is used, the intersecting layer's sources are merged in.

### `op="inline"` — inject LLM-provided rows

Args: `data: list[dict]` (required, ≤ 1,000 rows), `source: str` (see
below), `geometry_column: str | None`, `crs: str = "EPSG:4326"`,
`layer_name: str | None`.

**`source` is MANDATORY.** Provide it one of two ways:
- Top-level `source="Booli.se 2026-03 scrape"` — broadcast to every
  row. Use when all rows share one origin.
- Per-row `source` field on each dict — use when rows come from
  different origins (some from hitta.se, some from web search, some
  from the user). The top-level `source` fills gaps for rows that
  omit it.

Failing to provide either form returns `missing_arg`. Short specific
source strings are the norm ("SL.se timetable 2026-04", "hitta.se
manual lookup 2026-04-19"), not generic ones like "the internet" or
"web search".

If a column holds WKT strings, name it in `geometry_column` and it's
parsed + reprojected from `crs` → EPSG:3011. Resulting layers carry
`llm_sourced=True` in their provenance so `sources()` can surface
"this was made up by the model" as a first-class fact.

### Return shape (both ops)

`{loaded: [layer_summary, ...], errors: [{dataset_id, error, detail}],
n_loaded: N}`. Single-dataset loads still return a 1-element
`loaded` list for consistency.

---

## 4. `execute_sql(sql, description="", result_name=None, geometry_column=None)`

Run a validated, read-only SQL query against session layers. Single
SELECT / WITH / UNION only — DDL / DML is rejected by a
sqlglot-based parser. DuckDB spatial functions are available.
Session layers are referenced by their names as regular tables.

### Guardrails

- No semicolons, no multi-statement, no DDL, no DML.
- No HTTP URLs, no abs paths, no `read_*`/`copy_*` file functions.
- Numeric literals > 10M rejected as DoS protection (override by
  referencing a column or computing the value).
- 30-second wall-clock timeout (via `conn.interrupt()`).
- 256 MB memory cap per session.
- Rejects any identifier that doesn't resolve to a known session
  layer or standard DuckDB system table.

### Layer-vs-table decision

1. If `geometry_column` is set, layer mode is forced with that column
   cast to GEOMETRY.
2. Else the tool runs `DESCRIBE (sql)` and promotes to layer if any
   column has type starting with `GEOMETRY`.
3. Otherwise the query returns up to 50 rows as a markdown table. If
   a column named `geom` / `geometry` exists but got demoted to BLOB
   (common with cross-layer expressions), the response carries a
   `geometry_hint` telling you to retry with `geometry_column='...'`.

### Returns

- **Table mode** (no geometry detected): `{mode: "table", rows: <count>,
  capped_at, description, table_md, truncated?, warning?,
  geometry_hint?}`. The `table_md` field carries the rendered markdown
  table; `rows` is the row count (integer, not the rows themselves).
- **Layer mode**: a layer summary identical to `load` / `derive`.
  Pass `result_name` to name it explicitly; otherwise auto-generated.

---

## 5. `derive(op, ...)` — new layer from an existing one

Nine sub-ops, all producing a new session layer. Provenance inherits
from the source(s).

### Attribute filters

- **`op="filter"`** (`layer`, `where`, `result_name?`) — SQL WHERE
  → new layer. WHERE accepts any DuckDB-compatible predicate,
  including spatial ones on the `geom` column
  (`ST_Contains(geom, ST_Point(...))`, `ST_DWithin(geom, ..., 200)`).
  For "features of A that relate to any feature of B", prefer
  `op="select_by_location"` — it handles cross-layer predicates
  directly.
- **`op="top_n"`** (`layer`, `by`, `n=10`, `ascending=False`,
  `result_name?`) — filter + ORDER BY + LIMIT in one call. `by` is
  an SQL ordering expression
  (e.g. `"population"`, `"ST_Area(geom)"`, `"median_income DESC NULLS LAST"`).

### Spatial

- **`op="clip"`** (`layer`, `by_layer`) — trim `layer`'s geometries
  to the union of `by_layer`'s geometries. Geometries MODIFIED.
- **`op="intersect"`** (`a_layer`, `b_layer`) — geometric overlay:
  one row per intersecting pair, geometry = `ST_Intersection(a, b)`
  (often changes geometry kind). For a spatial join that keeps A's
  geometry unchanged, use `op="select_by_location"` instead.
- **`op="select_by_location"`** (`layer`, `by_layer`,
  `predicate="intersects" | "within" | "contains" | "dwithin"`,
  `distance_m?`) — classic spatial WHERE: keep features of `layer`
  (unchanged geometry + attributes) whose geom relates to ANY
  feature in `by_layer` by `predicate`. `dwithin` requires
  `distance_m`.
- **`op="buffer"`** (`layer`, `distance_m`) — `ST_Buffer` in
  EPSG:3011 metres.
- **`op="centroid"`** (`layer`) — per-feature `ST_Centroid`.
- **`op="dissolve"`** (`layer`, `by_columns?`) — union geometries;
  optionally grouped by attribute columns. Adds a `feature_count`
  column to the result.
- **`op="convex_hull"`** (`layer`, `aggregate=False`) — per-feature
  hull, or one aggregate hull for the whole layer with
  `aggregate=True`.

Returns a layer summary.

---

## 6. `edit_field(op, layer, ...)` — mutate a layer's columns in place

Five sub-ops. All are reversible inside an active checkpoint covering
`layer` (see `checkpoint`); without a covering checkpoint they're
permanent.

### `op="add"` (`name`, `expr`, `field_type?`)

Add a new column computed from a SQL expression. QGIS / ArcGIS
Field-Calculator pattern. The expression is evaluated per row and
may reference other columns of the same layer or scalar subqueries
against other session layers. Type inferred from the expression
result unless `field_type` is set
(`VARCHAR` / `DOUBLE` / `BIGINT` / `BOOLEAN` / `DATE` / `TIMESTAMP`).

Example: `edit_field(op="add", layer="buildings", name="area_m2",
                     expr="ST_Area(geom)")`.

### `op="update"` (`name`, `expr`, `where?`)

Overwrite an existing column's values from a SQL expression,
optionally restricted by WHERE. Same semantics as `op="add"` but
the column must exist. `where` limits which rows are updated; omitted
means all rows.

### `op="drop"` (`name`)

Remove a column. Refuses the geometry column — use `layer(op="drop", ...)`
for the whole layer.

### `op="classify"` (`name`, `rules=[{when, then}]`, `default?`)

Add a categorical column whose value is chosen from the first
matching rule. CASE-WHEN shorthand wrapping `op="add"`. Rules
evaluated in order; first match wins. `default` sets the value for
rows matching no rule (NULL if omitted).

```
edit_field(op="classify", layer="deso", name="income_band", rules=[
    {"when": "median < 300", "then": "low"},
    {"when": "median BETWEEN 300 AND 500", "then": "mid"},
    {"when": "median > 500", "then": "high"},
], default="unknown")
```

### `op="annotate"` (`values`, `key_column="rowid"`, `dry_run=False`, `model?`)

Attach LLM-classified per-feature attributes in one call. Columns
are created on the fly if they don't exist (type inferred from the
values: all-int → BIGINT, int/float mix → DOUBLE, bool → BOOLEAN,
else VARCHAR). Up to 10,000 keys per call. Pair with
`inspect(op="batch", ...)` for layers larger than you can reason
about in one pass.

```
edit_field(op="annotate", layer="buildings", values={
    "1": {"era": "functionalist", "confidence": 0.9, "note": "..."},
    "2": {"era": "art-nouveau",    "confidence": 0.7},
})
```

`key_column` defaults to `"rowid"` (DuckDB pseudo-column, stable
within a session). Use a declared key column when one exists.

`dry_run=True` previews coverage without writing — returns
`{dry_run: True, keys_matched, keys_unmatched, new_columns_would_create, ...}`.
Use this before committing large annotation payloads to catch
`key_column` mismatches.

`model="claude-opus-4-7"` (or similar) is stored in per-column
provenance so exported columns can be traced to their author.

The response reports both key-level matching (`keys_matched` /
`keys_unmatched`) and row-level coverage (`rows_total` /
`rows_with_any_annotation` / `rows_without_annotation`) so you can
distinguish "every key I sent hit a row" from "every row in the
layer received a value". The two differ when your `values` dict
covers only a subset.

Reversible inside a checkpoint (pre-image snapshotted once per
column, not per row).

---

## 7. `inspect(op, ...)` — explore the session

Four sub-ops. All `readOnlyHint`.

### `op="layers"` — session inventory

No arguments. Returns `{n_layers, layers: [{name, feature_count,
geometry_type, bbox_3011, columns, created_by, parent_layers, notes,
is_visible}, ...], active_checkpoint, open_checkpoints}`. Call this
when the LLM needs to recall what it has, or when it looks
overwhelmed by prior state.

### `op="rows"` (`layer`, `n=10`, `include_geometry=False`, `offset=0`, `where?`)

Sample rows from one layer as a markdown table. Hard caps: 200 rows
without geometry, 10 rows with geometry (WKT; verbose — request
only when needed).

Returns `{layer, rows_shown, rows_total, cap, table_md}` where
`table_md` is the rendered markdown (header + rows + a "N more not
shown" footer when applicable). The `where` clause is sqlglot-validated
before being spliced in.

For very large layers use `op="batch"` (resumable cursor).

### `op="batch"` (`layer` OR `cursor`, `columns?`, `batch_size=200`, `where?`)

Paginate through a layer with a resumable cursor. First call: pass
`layer` + optional `columns` / `where` / `batch_size`. Response
carries `rows`, `next_cursor`, `exhausted`. Subsequent calls: pass
`cursor=<next_cursor>` — all other args ignored.

Every batch includes a `rowid` column (DuckDB pseudo-column) suitable
for `edit_field(op="annotate", key_column="rowid", ...)`.

### `op="at"` (`points`, `radius_m=100.0`, `layers?`, `columns?`, `per_layer_limit=3`)

"What's near each of these points?" across session layers. Up to
**500 points per call**; `per_layer_limit` caps features per layer
per point (default 3; 1..25). For each point returns features
sorted by distance in metres.

`points` is always a list, even for a single point:

```
inspect(op="at", points=[{"id": "p1", "x_3011": 153844, "y_3011": 6578679}],
        radius_m=500)
```

`columns` lets you trim each returned feature to a specific attribute
set (keeps output small when you only need a name/id).

Returns `{points: [{id, x_3011, y_3011, results: [{layer, features: [{...}]}, ...]}, ...],
unknown_layers: [...], layers_considered: N}`.

---

## 8. `layer(op, ...)` — visibility + lifecycle

Five sub-ops.

### `op="show"` (`layers`, `title?`, `style?`)

REPLACE the viewer's visible set with `layers` (empty list = show
nothing). Pass `title=None` to preserve the existing panel title;
`""` to clear it. Returns `{title, viewer_url, visible_layers,
unknown_layers, styles}`.

The text response is fully usable on its own — opening the viewer
is optional.

**Style spec** — see the [Style spec](#show-thematic-styling) section
at the bottom. Invalid specs reject the whole call so you can fix
and retry in one round.

### `op="hide"` (`layers?`)

Remove specific layers from the visible set. With `layers=None` /
omitted, hides all.

### `op="rename"` (`name`, `new_name`)

Rename a layer. Reversible inside a covering checkpoint.

### `op="drop"` (`name`)

Remove a layer from the session. Reversible inside an active
checkpoint (full layer snapshotted); not reversible otherwise.

### `op="set_notes"` (`name`, `notes`)

Attach free-text narration to a layer. Surfaces in
`inspect(op="layers")` and `sources(layer)`. Useful for recording
*why* a layer exists ("filtered to pre-1940 stone buildings as a
proxy for the historical core") so the reasoning is recoverable from
session state alone.

---

## 9. `export(layers, format="gpkg", cite=False, ...)`

Export one or many session layers to a downloadable artefact.
Returns URL(s) valid for 24 h. `layers` accepts a single string or a
list.

### Formats

- **gpkg** (default, recommended): OGC GeoPackage in native
  EPSG:3011. Single-layer → one `.gpkg`; multi-layer → one `.gpkg`
  with every layer inside (QGIS / ArcGIS open it cleanly with each
  layer's geometry type and attributes preserved). One URL.
- **geojson**: EPSG:4326 FeatureCollection. Multi-layer emits one
  file per layer by default; pass `merge_geojson=True` for a single
  FeatureCollection where every feature has a `_layer` property
  (polygons / lines / points end up mixed — downstream consumers
  must tolerate it).
- **csv**: attribute columns + geometry as WKT.
- **parquet**: columnar, zstd-compressed, geometry as WKB.
- **png**: server-rendered styled map artefact. Honors the current
  `layer(op="show")` style. Paper-toned editorial backdrop (no tiled
  basemap — the server sandbox denies outbound egress, and the
  editorial palette reads cleaner than a Carto tile anyway). Args:
  `title?`, `legend=True`, `width_px=1600`, `height_px=1000`.

### `cite=True`

Bundle a provenance markdown block alongside the URL(s). Single-layer:
`citations_markdown` on the response. Multi-layer: same key carries
the deduped union across all input layers. Folds the one-call
"ship a publishable artefact" pattern.

### Return shape

- Single-layer non-png:
  `{layer, format, url, file_name, size_bytes, expires_at, provenance, ...}`.
- Multi-layer: `{files: [{url, size_bytes, layer?}, ...], format,
  expires_at, ...}` (plus `citations_markdown` when `cite=True`).
- PNG: `{url, format: "png", width, height, bbox_3011, size_bytes,
  expires_in_s, hint}`.

URLs are absolute when `GEODATA_PUBLIC_URL` is configured on the
server; otherwise relative (`/exports/<token>/<filename>`). Links
auto-expire after 24 h.

---

## 10. `sources(layer=None)`

Return a structured provenance report (publisher, licence, URL,
retrieval date, operations applied) for a layer or all session
layers, as markdown. Use this to cite where data came from after a
multi-step analysis.

Output sections per layer:
- Header: name, feature count, tool that created it.
- Derived from: parent layer names (recursively).
- Operations applied: timeline of tool calls with their descriptions.
- Source data: one block per upstream dataset (publisher, licence,
  URL, retrieved-on, llm_sourced flag).
- Notes: any free-text from `layer(op="set_notes")`.
- Column provenance: per-column author when columns were written by
  `edit_field` (with `model` parameter).

---

## 11. `checkpoint(op, name, ...)` — savepoints

Named savepoints that make in-place mutations reversible. A session
isn't a linear log of edits; it's a set of named savepoints that can
be rolled back independently. See `docs/design.md` for the rationale.

### `op="create"` (`name`, `layers?`)

Snapshot mutations going forward.

- `layers=None` (default): covers **every** layer. Any in-place
  mutation is tracked.
- `layers=["a", "b"]`: covers only those layers. Mutations to other
  layers are NOT snapshotted under this checkpoint — use a separate
  scoped checkpoint for them.

Multiple checkpoints can be active simultaneously. A mutation
covered by more than one active checkpoint is snapshotted for each.
Storage cost is column-scoped (O(changed columns × rows)), not
layer-wide. A checkpoint on a 79k-row layer that only rewrites two
columns stores two 79k-row columns, not 158k duplicates.

### `op="rollback"` (`name`)

Restore every mutation made since `op="create"` for this name.
Discards the checkpoint and its snapshots.

### `op="commit"` (`name`)

Make all mutations since `op="create"` for this name permanent.
Discards snapshots, reclaims storage.

### Covered mutations

`edit_field` (add / update / drop / classify / annotate), `layer`
(rename / drop). `load` / `execute_sql(result_name=...)` / `derive`
create new layers and are not "mutations" in the checkpoint sense
(drop the resulting layer if you want to undo them).

---

## Cross-cutting: canonical SCB join keys

Every `scb_*` parquet now carries these six columns in addition to
the raw `region` / `region_kind` / `region_code` / `region_name`:

- `desokod`: DeSO code (9 chars), equals `region_code` when
  `region_kind='deso'`. Join to `deso_2025.desokod` / `deso_2018.desokod`.
- `desokod_2025`: 2025-grid equivalent of `desokod`. Bridges 2018 →
  2025 via `deso_historical_changes`. Equals `desokod` when the
  DeSO is unchanged since 2018.
- `regsokod`, `regso_name`: parent RegSO. Joined via
  `deso_regso_mapping`.
- `kommunkod`, `kommun_name`: parent kommun. Joined via
  `deso_regso_mapping`.

All six are NULL for non-DeSO rows (RegSO / kommun / country).
Prefer them over the raw `region` column for joins; they're uniform
across the 31 SCB tables.

---

## Richer default responses

`load`, `derive`, `execute_sql` (layer mode), and the spatial ops
return a `quick_stats` block and a 3-row `sample` by default, in
addition to the existing feature_count / bbox / attributes /
provenance. Skipped for layers above `GEODATA_QUICK_STATS_CAP` (200k
features by default). Saves the typical "call stats() and inspect()
after load" round-trip.

---

## `layer(op="show")` thematic styling

`layer(op="show", layers=[...], style={...})` accepts a per-layer
style spec:

```
layer(op="show", layers=["buildings"], style={
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
Categorical with no palette auto-assigns from a default set.

Four optional channels per layer:

- **color** (via `column` + `scale` + `palette`) — required for any
  non-default rendering.
- **size**: `{"column": "<attr>", "range": [lo, hi]}` — linear map
  of numeric column → marker size (points/lines only).
- **opacity**: `{"column": "<attr>", "range": [lo, hi]}` — 0..1.
- **stroke**: `{"column": "<attr>", "range": [lo, hi]}` — stroke
  width (polygons/lines only).

Use them to double-encode features (e.g. color = category, size =
importance). Unstyled layers keep their default solid color. Styles
persist in session state and are consumed automatically by the
viewer's 2 s auto-refresh poll.

---

## Error codes

Every error response has `error`, `detail` (prefixed
`[server-side]`), and `origin` fields. Treat them as coming from the
MCP server's host, not the client's local machine. Do NOT try to
mkdir/chmod/debug paths locally — any filesystem references are on
the server.

- `unknown_dataset` — `catalog(id=...)` or `load(op="catalog")` with
  a bad id. Call `catalog(query=...)` to list available datasets.
- `sql_rejected` — `execute_sql` validator blocked the SQL; `detail`
  names the rule.
- `sql_failed` — `execute_sql` hit DuckDB execution error; check
  column names / types.
- `op_failed` — generic operation error; `detail` explains.
- `missing_arg` — required argument for the chosen `op` was missing.
- `unsupported_operation` — `op=...` value isn't one of the accepted
  literals. Response carries `supported` with the valid set.
- `too_many_points` — `inspect(op="at")` with > 500 points. Batch
  smaller.
- `unknown_layer` — referenced a layer name not in the session.
  Response carries `available` listing what IS in the session.
- `invalid_style` — `layer(op="show")` style spec rejected;
  `detail` names the offending key.
- `no_match` — `geocode(op="bbox")` found nothing inside Stockholm
  coverage.
- `session_expired` — idle > 30 min; response carries a `replay`
  block with the operation log so you can reconstruct.
