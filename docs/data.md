# Data model

What's in the catalog, how it's normalized, and the join-key decisions
that cut cross-table joins from hand-rolled SQL to single-table queries.

---

## Coverage and CRS

Everything is pre-filtered to Stockholm kommun (`kommunkod = '0180'`) and
reprojected to **EPSG:3011** (SWEREF 99 18 00). This is a deliberate
choice: the working reference frame is the one city planners use, with
units in metres and no coordinate-order ambiguity. Consumers do not have
to reason about CRS conversions during analysis; the viewer handles the
EPSG:4326 export at the edge.

Pre-filtering happens in `scripts/normalize.py`. Raw downloads from the
various source portals land in `data/raw/`, get cleaned up, and write to
`data/normalized/<source>/...` as parquet (tabular) or gpkg (spatial).

---

## The 66 datasets

Three primary source families:

- **SCB (Statistics Sweden).** Demographic and economic tables on DeSO
  (*demografiska statistikområden*, the standard Swedish small-area
  geography). Population, income, age structure, household composition,
  education, employment. All tabular; join on `desokod` or its 2025
  equivalent.
- **SBK Stadskarta (Stockholm City Planning Office).** City plan
  vector layers: admin polygons (stadsdel/kvarter), buildings,
  installations, roads, lines, points, and a name-label layer used for
  geocoding. Mostly polygons; some lines and points.
- **OSM (OpenStreetMap).** Addresses, ingested via Geofabrik's Sweden
  PBF extract, filtered to kommunkod 0180 and normalized to point
  geometries + address components.

Plus the polygon layers for DeSO itself (`DeSO_2018.gpkg` and
`DeSO_2025.gpkg`) which sit outside the normalization pipeline because
they're already in the right shape.

Call `catalog(query=...)` to fuzzy-find by name, description, or
keyword. Call `catalog(id=...)` for the full attribute schema.

---

## The normalization pipeline

Input: raw source files. Output: parquet (for tabular) or gpkg (for
spatial), plus a catalog entry with sample values for every attribute.

Per-source responsibilities:

- **SCB:** fetch the open-data XLSX/CSV tables, resolve DeSO codes,
  reproject the DeSO polygon geometry, add canonical join keys (see
  below), write parquet.
- **SBK:** read the `Stadskarta_hela_Stockholm.zip` archive, extract the
  individual geopackages, reproject where necessary, filter to
  kommunkod 0180, write to `data/normalized/sbk/`.
- **OSM:** DuckDB's `ST_ReadOSM` over the Geofabrik PBF, filtered by
  bounding box, `kommun=Stockholm`-match via a spatial join against
  kommun polygon, addresses extracted and pivoted into normal form.

Everything runs idempotently; rerunning `normalize.py --scb` doesn't
corrupt anything, just overwrites. `catalog_audit.py` and
`cross_ref_audit.py` run at the tail of the pipeline to verify:

- Every catalog entry points at a file that exists.
- Every attribute declared in the catalog is present in the file.
- Sample values in the catalog match what's actually in the data.
- DeSO codes referenced in descriptions exist in the DeSO polygon layer.

Both scripts exit 0 in CI; non-zero means a manual edit broke something.

---

## Canonical join keys

The single most impactful normalization decision.

Every SCB table carries these join columns, populated where the row's
`region_kind == 'deso'` and NULL otherwise:

- `desokod`: the DeSO code from the 2018 boundary set (the common join
  axis).
- `desokod_2025`: the 2025-boundary equivalent, looked up via
  `deso_historical_changes.parquet`. If the 2018 code maps to multiple
  2025 codes (a split), we pick the first and annotate in the audit
  log how many rows were bridged vs left alone.
- `regsokod`: RegSO code (aggregates DeSOs into slightly larger areas),
  from `deso_regso_mapping.parquet`.
- `regso_name`: human-readable RegSO name.
- `kommunkod`: municipality code (always `'0180'` since we pre-filter,
  but kept for multi-kommun extensions).
- `kommun_name`: human-readable municipality name (always `'Stockholm'`).

The raw source columns (`region`, `region_code`, `region_name`) are kept
alongside as provenance. The canonical columns are additive.

**Why this matters.** Before this pass, cross-table joins meant remembering
that `scb_be0101_deso_befolkning` uses `region`, `scb_be0101_deso_kon` uses
`Region`, and `deso_polygon_2018` uses `deso`. A stats query across three
SCB tables plus a DeSO polygon would take 4-5 turns of the LLM guessing
column names wrong. After the pass, any query is:

```sql
SELECT p.name, b.count, i.median_income
FROM scb_be0101_deso_befolkning b
JOIN scb_be0101_deso_income    i ON b.desokod = i.desokod
JOIN deso_2025                 p ON b.desokod_2025 = p.desokod
WHERE p.name = 'Södermalm'
```

No column-name guessing. The LLM writes it right the first time.

This was the single highest-ROI item in the post-demo feedback.

---

## DeSO 2018 → 2025 bridge

In 2023, Sweden redrew DeSO boundaries. The 2018 and 2025 polygon sets
overlap but aren't identical: some were split, some merged, some
unchanged.

Every SCB table that carries 2018 DeSO codes also carries a
`desokod_2025` column derived from `deso_historical_changes.parquet`.
Rules:

- 2018 code unchanged in 2025 → `desokod_2025 = desokod`.
- 2018 split into several 2025 codes → we pick the first code from the
  mapping (arbitrary but stable). Audit logs count how many rows got
  bridged this way so downstream analyses know the fidelity.
- 2018 merged into a 2025 code → straightforward.

Analyses that need rigorous 2018-vs-2025 semantics should use the
bridge column as a join target against `deso_2025` polygons. Analyses
that are fine with the 2018 boundaries can keep `desokod`.

---

## The dedupe-hint mechanism

Some datasets have non-obvious row-level quirks. Example:
`sbk_placenames` stores multi-word labels as *one point per word*.
`Högsta förvaltningsrätten` appears as three points with TEXTSTRING
values `Högsta`, `förvaltningsrätten`, and so on. Every LLM that touches
this dataset will rediscover the quirk the same way (confused queries,
then a GROUP BY workaround).

The fix: a `dedupe_hint` field on the catalog entry that surfaces the
caveat up-front in `catalog(id=...)` output:

```
dedupe_hint: "Multi-word labels are one point per word. Use
GROUP BY NAMN, GRUPP with ST_Centroid(ST_Collect(list(geom))) to get
one point per named thing."
```

The LLM sees this before loading, avoids the trap. Cheap to add; meaningful
UX.

---

## Column typing

DuckDB types are preserved through normalization and loading. For the LLM's
benefit, types are reported alongside column names in `catalog(id=...)`
and `_layer_summary`, so the LLM can pick correctly between `COUNT(x)`
and `SUM(x)` without probing.

Geometry columns use DuckDB's `GEOMETRY` type (via the spatial extension).
Queries auto-detect geometry columns in `execute_sql` so the LLM doesn't
have to declare them explicitly except in rare cases where DuckDB demotes
the result to BLOB after a cross-layer expression.

HUGEINT columns (sometimes produced by aggregations) get auto-coerced to
BIGINT or DOUBLE on export to avoid the cryptic "precision up to 19"
error that surprises downstream consumers.

---

## Attribute sample values

Every catalog entry's attribute list carries up to 5 `sample_values` per
column, populated automatically by `catalog_autogen.py`. These show up
in `catalog(id=...)` responses. They exist because an LLM looking at a
column called `KATEGORI` has no idea whether the values are English or
Swedish, free-form or closed-set, without them.

With them, the LLM sees:
```
KATEGORI: VARCHAR: Stadsdel, Kvarter, Distrikt, Bostadsfastighet, ...
```
and can write the right WHERE clause first try.

---

## Why parquet-for-tabular, gpkg-for-spatial

- **Parquet** for everything without geometry. Column-oriented, zstd
  compressed, fast DuckDB ingest, tiny on disk.
- **GeoPackage** for geometry-bearing layers. DuckDB spatial reads them
  cleanly via GDAL, they open in ArcGIS or QGIS without setup, and a single file
  can carry multiple layers.

Shapefile is explicitly not used even for archival: the column-name length
limit (10 characters), single-polygon-per-feature representation, and
character encoding quirks make it a bad archive format.

---

## Licences

Every `SourceRef` carries a licence string. Major sources:

- **SCB data:** usually *CC BY 4.0* or equivalent open licences (varies
  per table; the canonical per-table licence is captured in the catalog).
- **SBK Stadskarta:** *CC0 1.0* (public domain dedication via Stockholm's
  open-data portal).
- **OSM:** *ODbL 1.0*. Requires attribution + share-alike for derived
  products; `sources(layer)` surfaces this.
- **DeSO boundaries:** *CC0 1.0* from SCB.

Mixing these is usually fine for non-commercial and public-sector use,
but OSM's ODbL share-alike means anything derived from OSM has to be
published under ODbL too. The `export(format="png")` artefact doesn't
technically trigger ODbL (it's not a "substantial extract of the
database"), but a derived parquet absolutely does. Always check
`sources(layer)` before publishing.
