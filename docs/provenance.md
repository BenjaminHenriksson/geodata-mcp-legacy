# Provenance

Provenance is two-level: *layer-level* (inherited from the data source)
and *column-level* (tracks in-session authoring). Together they let any
attribute in any layer be traced back to its origin without
out-of-band notes.

---

## Layer-level provenance

Every `LayerMeta` carries a `provenance: list[SourceRef]`.

A `SourceRef` looks like:

```python
SourceRef(
    dataset_id="scb_be0101_deso_befolkning",
    source_name="SCB — Befolkning per DeSO, ålder och kön",
    publisher="Statistiska Centralbyrån",
    license="CC BY 4.0",
    url="https://www.scb.se/en/finding-statistics/...",
    file_path="data/normalized/scb/be0101_deso_befolkning.parquet",
    retrieved="2026-04-17",
    llm_sourced=False,
    llm_source_description=None,
)
```

When `load(dataset_id='x')` runs, it copies the catalog entry's source
into the new layer's `provenance`. When `filter`, `spatial`, `execute_sql`,
or a macro helper derives a new layer, it inherits the union of its
parents' provenance (deduped by `dataset_id`).

So any layer three operations deep still knows its original sources.
`sources(layer)` renders the full list with licence, URL, and file path.

### LLM-sourced layers

`load(op="inline")` flags geometry produced by the model (typed
coordinates, hand-built polygons) as not having a provenance chain
back to open data — the `source` argument is mandatory and every row
carries it. The rendered `sources()` output says:

```
**LLM-sourced**: model-drawn polygons
    — "Rough extents of planned construction, per council meeting notes"
```

Everything derived from that layer inherits the flag. Anyone looking at
the citation report can see that part of this analysis leans on model
output.

---

## Column-level provenance

`LayerMeta.column_provenance` is a dict from column name to an
authoring record:

```python
{
    "era":  {"authored_by": "llm",     "tool": "annotate",
             "at": "2026-04-19T16:32:01Z", "model": "claude-opus-4-7"},
    "slope_deg":  {"authored_by": "derived", "tool": "add_field",
                   "at": "2026-04-19T16:33:05Z",
                   "expr": "ST_Area(geom) / 1000"},
}
```

The `model` field is whatever string the calling client passes to
`annotate(model=...)`: `claude-opus-4-7`, `gpt-5`,
`gemini-2.5-pro`, `qwen3-max`, anything. The server doesn't validate
it; it just records it so the downstream reviewer can see which model
wrote which column.

Columns absent from this dict are assumed to come from the loaded
source (no in-session authoring). `edit_field` with
op ∈ {add, update, classify, annotate} writes into this dict;
op="drop" removes.

### Why two provenance levels

The two levels answer two different questions:

- **"Where did this table come from?"** Layer-level. Answered by
  `provenance: list[SourceRef]`.
- **"Who wrote this specific column?"** Column-level. Answered by
  `column_provenance: dict`.

A layer loaded from `sbk_admin_polygons`, filtered to KATEGORI =
'Stadsdel', annotated with an LLM-classified `era` column, and then
extended with a derived `area_m2` column, has:

- `provenance = [SourceRef(sbk_admin_polygons, ...)]`
- `column_provenance = {"era": {authored_by: llm, model: ...},
                        "area_m2": {authored_by: derived, expr: ...}}`

`sources(layer)` renders both sections so a reviewer can see the source
and the in-session edits in one place.

---

## How it appears in tool responses

### `sources(layer)` (markdown)

```
### Layer: `my_annotated_stadsdelar`  (117 features, created by `edit_field`)
Derived from: `sbk_admin_polygons`

**Operations applied** (root → result):
- `load(op='catalog', dataset_ids=['sbk_admin_polygons'])` → `sbk_admin_polygons`: loaded
- `derive(op='filter', layer='sbk_admin_polygons', where="KATEGORI='Stadsdel'")` → ...
- `annotate(layer='sbk_admin_polygons', n_keys=117,
            attributes=['era'], key_column='NAMN')` → annotated 117/117 keys
- `edit_field(op='add', layer='sbk_admin_polygons', name='area_m2', expr='ST_Area(geom)')`
              → added area_m2

**Sources** (deduplicated):
1. **SBK Stadskarta — Administrativa polygoner** (`sbk_admin_polygons`)
   - Publisher: Stockholms stad, stadsbyggnadskontoret
   - License: CC0 1.0
   - URL: https://dataportalen.stockholm.se/...
   - File: `data/normalized/sbk/admin_polygons.gpkg`
   - Retrieved: 2026-04-17

**Column attribution** (columns written in-session, distinct from the
layer's loaded sources above):
- `area_m2` — derived via `edit_field(op='add')` at 2026-04-19T16:33:05Z
    expr: `ST_Area(geom)`
- `era` — llm via `annotate` at 2026-04-19T16:32:01Z (model: claude-opus-4-7)
```

### `_layer_summary` (dict, returned by load/filter/show/etc.)

```python
{
    "name": "...",
    "feature_count": 117,
    "provenance": [{"dataset_id": ..., "source_name": ..., ...}],
    "column_provenance": {
        "era": {"authored_by": "llm", ...},
        "area_m2": {"authored_by": "derived", ...},
    },
    ...
}
```

The viewer reads `column_provenance` and renders an "authored" line per
layer, so the user seeing the map knows which columns to be suspicious
of.

---

## How exports carry provenance

`export(layer, format=...)` returns:

```python
{
    "url": "https://.../exports/<token>/<filename>",
    "format": "gpkg",
    "size_bytes": 3412992,
    "provenance": [...same SourceRef list...],
    "license_summary": "CC0 1.0 (sbk_admin_polygons)",
    "expires_in_s": 86400,
}
```

So the user downloading the file has the citation info in the same
response. The file itself (GPKG or parquet) carries the data; the
provenance lives in the tool response, which the LLM can embed in its
message.

Column-level provenance is surfaced in the response but not embedded
in the file format itself. GeoPackage supports `gpkg_metadata` tables
for this but wiring them is future work. The current path: the LLM
records the per-column authoring in its narrative around the download
link.

---

## Trust model implications

### What provenance does

- Lets a downstream consumer check the licence before republishing.
- Makes "this column was added by a model" visible. In a municipal
  context that's a non-trivial distinction.
- Survives transformations (filter → spatial → macro → export) because
  provenance is inherited at every step.
- Gives the reviewer a reproducibility trail even if session state is
  later lost. `history` + `provenance` + `column_provenance` are
  enough to rebuild the analysis from the original datasets.

### What provenance does not do

- **Does not verify that the model's annotation is correct.** An LLM
  can write wrong values into an `era` column; provenance only records
  *that* the LLM wrote them, not whether they're right.
- **Does not defend against deliberate tampering.** A malicious caller
  can `edit_field(op="update", ...)` to overwrite genuine values and
  the column provenance records the override but doesn't prevent it.
- **Does not cryptographically sign exports.** The citations are plain
  text in the response; a downstream actor could strip them. If
  non-repudiation becomes a requirement, we'd need signing.

For the current use case (semi-trusted LLM callers, open-data inputs,
internal review of outputs), this is enough.

---

## Operation-level audit (per-call SQL trail)

Layer- and column-level provenance say "where did this column / dataset
come from". The audit log says "what SQL did the LLM actually run, in
which tool call, and what was its description". Together they answer
both "is this data trustworthy at its source?" and "do I trust the
specific transformation the LLM applied?".

Every mutating MCP tool runs inside `Session.audit_context(tool,
description)`, which mints a `correlation_id` and propagates it to:

- every SQL statement executed inside the tool body (captured by the
  `_AuditedConnection` proxy around the DuckDB connection);
- the `Operation` record logged for that call.

The user-supplied `description` argument flows through to the resulting
`Operation` and shows up in the viewer's audit panel together with the
captured SQL. Operations without a description are flagged "no
description provided", a visible nudge.

Records are mirrored to `<sid>.audit.jsonl` (append-only) and served
to the viewer at `GET /api/<sid>/audit_log`. See [Sessions](sessions)
and [Viewer](viewer) for the on-disk format and panel UX respectively.

---

## Trade-offs

### Every op carries provenance plumbing

Code is slightly heavier than a minimal implementation. A typical
derivation like `derive(op="filter", layer, where)` has to read the
parent's provenance and write it into the derived `LayerMeta`.
`column_provenance` adds a stamp-on-write step to every
attribute-mutating op. Accepted cost.

### Column-provenance overwrites on re-annotate

If an LLM annotates a column, then another LLM re-annotates the same
column, the second authoring wins; the old record is lost. We don't
keep a revision history at the column level. For debugging, the
operation history has the full sequence; for trust, the current
authoring is what matters.

### GeoPackage metadata tables not used

GPKG supports `gpkg_metadata` + `gpkg_metadata_reference` for richer
lineage storage. We don't use them because writing them from DuckDB's
OGR bridge is awkward and the column-provenance JSON in the tool
response is sufficient today. Future work if a consumer specifically
needs in-file metadata.
