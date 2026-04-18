# Geodata MCP

An MCP (Model Context Protocol) server that gives LLMs interactive access to
Swedish open geodata — **65 datasets** of Stockholm Stadsbyggnadskontorets city
map, SCB demographic statistical areas (DeSO 2018 + 2025), and SCB's DeSO-keyed
statistical tables — with a companion viewer.

> **Current state: public endpoint is OFFLINE.** The service was stopped and
> disabled as part of a security review after an adversarial audit of
> `execute_sql` surfaced reachable file-read / SSRF / DoS primitives. All
> three mitigation layers (sqlglot denylist, DuckDB resource caps, systemd FS
> sandbox) are implemented and tested in this repo; the service is paused
> pending my review of whether to keep the bearer-auth-only exposure model or
> lock the endpoint down further. **See [`docs/security.md`](docs/security.md)
> for the full write-up**, and [`docs/deployment.md`](docs/deployment.md) for
> the procedure to re-enable it.
>
> `https://benjaminhenriksson.com` (the root domain / personal site) is
> unaffected. Only `https://geo.benjaminhenriksson.com` is offline.

- **When live:** `https://geo.benjaminhenriksson.com`
- **MCP endpoint:** `POST /mcp` (bearer auth, streamable HTTP)
- **Viewer:** MapLibre GL JS, dark Carto basemap, click popups, per-layer toggles, optional user basemap key
- **License of underlying data:** CC0 1.0 (SCB) + CC0 1.0 (Stockholm SBK open-data variant) + CC BY 4.0 (Lantmäteriet — not currently ingested)
- **Code license:** proprietary, all rights reserved (see `LICENSE`)

---

## What it does

26 tools across four categories. All carry MCP `toolAnnotations`
(`readOnlyHint` / `destructiveHint=false`) so clients like Claude Code /
Desktop can auto-approve safe calls.

**Discovery & read-only**
| Tool | What it does |
|---|---|
| `search_data` | Fuzzy-search the 65-dataset catalog (SV/EN names, descriptions, keywords) |
| `geocode` | Place-name + composite street-number lookup against SBK labels + polygons |
| `list_layers` | Inventory of all session layers + notes + checkpoint state |
| `inspect` | Sample rows from any layer (200 attribute cap / 10 with geometry) |
| `inspect_location` | "What's here?" — features within N m of a point across many layers in one call |
| `batch_iterate` | Cursor-paginated read for layers too large to inspect in one shot |
| `stats` | Aggregation tables (min/avg/max/count, grouped) |
| `sources` | Walks the provenance chain → markdown citations |

**Layer creation & session state**
| Tool | What it does |
|---|---|
| `load` | Read one catalog dataset into the session; bbox/attribute/intersect filters |
| `load_many` | Bulk-load several datasets in one call |
| `filter` | SQL WHERE on an existing session layer → new layer |
| `spatial` | `select_by_location` / `clip` / `intersect` / `buffer` / `centroid` / `dissolve` / `convex_hull` |
| `execute_sql` | Read-only DuckDB SQL escape hatch, sqlglot-validated, 30 s timeout |
| `create_layer` | Inject LLM-provided data as a layer (1 k row cap, WKT geom, `llm_sourced=True`) |
| `export` | Write to GeoJSON/GPKG/CSV/Parquet, 24 h download URL |
| `show` | Mark layers visible in the viewer |
| `set_notes` | Attach free-text narration to a layer (surfaces in `list_layers`/`sources`) |

**In-place layer mutation** (QGIS Field-Calculator pattern; reversible in a checkpoint)
| Tool | What it does |
|---|---|
| `add_field` | Add a new column computed by a SQL expression |
| `update_field` | Overwrite an existing column; optional WHERE |
| `drop_field` | Remove a column (not the geometry column) |
| `annotate` | Bulk per-feature LLM-classified attributes: `{id: {attr: val, ...}}` |
| `drop_layer` | Remove a layer from the session |
| `rename_layer` | Rename a layer |

**Transaction control**
| Tool | What it does |
|---|---|
| `checkpoint` | Create a named checkpoint — subsequent mutations become reversible |
| `rollback` | Undo every mutation since `checkpoint(name)` |
| `commit` | Make mutations permanent, discard snapshots |

Full tool reference: **[`docs/tools.md`](docs/tools.md)**. The server also
publishes top-level `instructions` at connection time — a ~4 KB workflow
primer Claude (and other MCP clients) read before the first tool call.

---

## Reproducing the dataset from scratch

This repository does **not** ship data. Raw downloads (~8.9 GB) and
normalized derivatives (~1 GB) are excluded via `.gitignore`; everything
under `data/` is regenerable from the pipeline.

```bash
# 1. Pull raw source data into data/scb_deso/ and data/stockholm_sbk/
#    per the URLs in sources.md. Both are CC0 1.0. No API key required.

# 2. Normalize: Windows-1252 → UTF-8, EPSG:3006 → EPSG:3011, filter to
#    Stockholm (kommunkod 0180), typed parquet, whitespace strip, dedup.
#    Runs catalog_audit.py + cross_ref_audit.py at the end.
uv run --with pandas --with pyarrow --with openpyxl \
  python scripts/normalize.py
```

Expected output layout and sizes are documented in
[`DATA_SUMMARY.md`](DATA_SUMMARY.md). The Phase-0 exploration scripts
(`scripts/deso_vs_stockholm.py`, `filter_estimate.py`, `overlap_check.py`)
are retained as one-shots that informed the filtering decisions.

---

## Quick start for MCP clients

### 1. Get the bearer token

```bash
ssh geo-vps 'sudo grep GEODATA_MCP_TOKEN /etc/geodata-mcp.env'
```

### 2. Claude Code

```bash
claude mcp add --transport http geodata \
  https://geo.benjaminhenriksson.com/mcp \
  --header "Authorization: Bearer $TOKEN"
```

Restart Claude Code. The 12 tools appear automatically.

### 3. Claude Desktop (via the `mcp-remote` shim)

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "geodata": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "https://geo.benjaminhenriksson.com/mcp",
        "--header", "Authorization:Bearer <TOKEN>"
      ]
    }
  }
}
```

### 4. Any other MCP-compliant client

Streamable HTTP at `https://geo.benjaminhenriksson.com/mcp`, bearer auth in the
`Authorization` header. Tested with the `fastmcp.Client` Python SDK; any
MCP 2024-11-05+ client should work.

### 5. Viewer

After an MCP client calls `show([...])`, the tool returns a `viewer_url` like
`/view/<uuid>`. Open `https://geo.benjaminhenriksson.com/view/<uuid>` in any
browser. You get a MapLibre map with click-to-inspect popups and per-layer
toggles. The session's URL is unguessable per connection.

---

## Example sessions

### "What's the income around Tekniska nämndhuset?"

```python
# Via any MCP client, in natural language the LLM emits:
geocode(name="Tekniska nämndhuset")                      # → (152699, 6579781)
load(dataset_id="deso_2025", layer_name="deso")
execute_sql(sql="""
  SELECT desokod FROM "deso"
  WHERE ST_Contains(geom, ST_Point(152699, 6579781))
""")                                                      # → 0180C4220

load(dataset_id="scb_income_structure", layer_name="income")
execute_sql(sql="""
  SELECT år, "Inkomststruktur nettoinkomst" AS mean_tkr
  FROM "income"
  WHERE region = '0180C4220' AND inkomstkomponent='nettoinkomst'
    AND tabellinnehåll='Medelvärde för samtliga, tkr' AND kön='totalt'
    AND "Inkomststruktur nettoinkomst" IS NOT NULL
  ORDER BY år DESC LIMIT 3
""")

sources(layer="deso")   # → markdown citation block
```

Actual answer: this DeSO's mean net income in 2024 was **632 tkr** (Stockholm
kommun average 453 tkr; national 358 tkr), ~83rd percentile among 544 Stockholm
DeSOs. See `docs/tools.md` for the raw transcript.

### "Show me all buildings in Södermalm"

```python
load(dataset_id="sbk_admin_polygons", layer_name="admin")
filter(layer="admin", where="KATEGORI='Stadsdel' AND NAMN='SÖDERMALM'",
       result_name="sodermalm")
load(dataset_id="sbk_buildings", intersect_layer="sodermalm",
     layer_name="bldg_sodermalm")
show(layers=["sodermalm", "bldg_sodermalm"])
```

3,173 buildings clipped by the actual Stadsdel polygon.

### "Where do my favorite cafés sit in the DeSO grid?"

```python
create_layer(name="favorite_cafes",
             data=[{"name": "Drop Coffee", "rating": 4.6, "geom_wkt": "POINT(18.0658 59.3177)"},
                   {"name": "Café Pascal", "rating": 4.4, "geom_wkt": "POINT(18.0525 59.3427)"}],
             source="Personal visits 2026-04", geometry_column="geom_wkt")
execute_sql(sql="""
  SELECT c.name, c.rating, d.desokod
  FROM "favorite_cafes" c JOIN "deso" d ON ST_Contains(d.geom, c.geom)
""")
export(layer="favorite_cafes", format="gpkg")
sources()
```

---

## Datasets (65 total)

- **DeSO 2018** + **DeSO 2025** polygons (Stockholm), EPSG:3011
- **DeSO 2018↔2025 historical changes** (1,234 rows) — SCB's official migration mapping
- **DeSO↔RegSO connection table** (6,160 rows)
- **30 SBK Stadskarta layers** — buildings, addresses, place names, administrative areas, streets, water, contours, etc. (Stockholm kommun, EPSG:3011)
- **31 SCB statistical tables** keyed by DeSO/RegSO/kommun/Riket — population, households, income, employment, housing stock, buildings-by-age, land use, cars (Stockholm DeSOs + Stockholm RegSOs + country aggregate)

Run `search_data("")` to list them all. Full source notes in
[`DATA_SUMMARY.md`](DATA_SUMMARY.md) and [`sources.md`](sources.md).

---

## Architecture

```
MCP client / browser
  │ HTTPS (Let's Encrypt via Caddy + Cloudflare proxy)
  ▼
Cloudflare edge → Caddy → geodata-mcp service on 127.0.0.1:8765
                         │
                         ├── FastMCP 3.x (streamable HTTP)
                         │     per-connection session UUIDs
                         │     12 tools
                         │
                         ├── DuckDB 1.5 + Spatial
                         │     256 MB memory_limit per session
                         │     30 s statement timeout via interrupt()
                         │
                         ├── Starlette viewer/API routes
                         │     /view/<sid>, /api/<sid>/..., /exports/<token>/...
                         │
                         └── Background session GC
                               idle > 30 min → dump log to /tmp/geodata_sessions/<sid>.json
                               next touch → structured SessionExpired with replay info
```

- Per-IP rate limit: 120 req/min, burst 40 (belt-and-braces; Cloudflare is the real guard).
- Token bearer auth on `/mcp/*` only. Viewer/API ride on the session UUID.
- All data locally in `data/normalized/` (EPSG:3011 for spatial, UTF-8 for text).

Full deployment details: **[`docs/deployment.md`](docs/deployment.md)**.

---

## Development

```bash
# first-time clone
git clone …  geodata-mcp
cd geodata-mcp
uv sync

# regenerate normalized data (raw files must be present under data/)
uv run --with pandas --with pyarrow --with openpyxl \
  python scripts/normalize.py

# run locally (stdio — plug into Claude Code config)
uv run python -m geodata_mcp

# run HTTP (same as production)
uv run python -m geodata_mcp --http --host 127.0.0.1 --port 8765
```

### Audits

Two CI-ready audits live in `scripts/`:

- `catalog_audit.py` — every catalog `sample_values` entry must appear in the data, feature counts must match, no undeclared columns without explanation.
- `cross_ref_audit.py` — every DeSO `region` code across SCB tables must resolve to a polygon in the 2018 grid, 2025 grid, or SCB's historical-changes mapping.

Both are run automatically at the end of `scripts/normalize.py` and fail the
pipeline on regressions.

### Layout

```
geodata-mcp/
├── catalog.json                — 65 datasets, LLM-facing
├── sources.md                   — original source URLs
├── DATA_SUMMARY.md              — human-readable catalogue
├── pyproject.toml + uv.lock
├── geodata_mcp/
│   ├── server.py                — FastMCP + Starlette + 12 tools + routes
│   ├── catalog.py               — rapidfuzz-backed catalog search
│   ├── session.py               — per-connection sessions, GC, persistence
│   ├── loader.py                — read normalized data → DuckDB
│   ├── operations.py            — Phase 2/3 ops (filter/spatial/stats/sql/sources/create_layer/export)
│   └── geocoder.py              — SBK-backed place + street+number lookup
├── viewer/
│   ├── index.html
│   └── app.js
├── data/
│   ├── scb_deso/…               — raw SCB DeSO geopackages + CSVs + SCB mappings
│   ├── stockholm_sbk/…          — raw SBK shapefiles
│   ├── normalized/              — Phase 0 output, canonical EPSG:3011 + UTF-8
│   │   ├── sbk/*.gpkg           — 30 files
│   │   ├── deso/*.gpkg          — 2 files (2018 + 2025)
│   │   ├── scb/*.parquet        — 31 files
│   │   └── mappings/*.parquet   — 2 files (historical changes + DeSO↔RegSO)
│   └── exports/<token>/         — per-request downloads (24 h TTL)
├── scripts/
│   ├── normalize.py             — Phase 0 preprocessor (raw → normalized)
│   ├── catalog_audit.py
│   ├── cross_ref_audit.py
│   ├── catalog_autogen.py       — regenerate catalog entries from normalized files
│   └── probe.py                 — emit raw-file metadata.json (Phase 0 artifact)
├── docs/
│   ├── tools.md                 — full tool reference
│   ├── deployment.md            — VPS / Caddy / Cloudflare deploy notes
│   └── security.md              — threat model, sandbox layers, re-enable checklist
└── README.md                    — this file
```

---

## Known limitations

- **Coverage is Stockholm kommun** — everything is filtered to kommunkod `0180`. Expanding is mostly a data-ingest problem, not a tool-design problem.
- **Street + house-number geocoding** pairs via 250 m spatial join between the street-label point and the nearest address-number point. Some pairs are off-by-one-building.
- **SCB privacy suppression** — small-population DeSOs have NULL values in statistical tables. Catalog descriptions warn about this; queries must `WHERE value IS NOT NULL` to skip them.
- **38 catalog warnings** — SBK cartographic columns (TEXTFONT, TEXT_ANGLE, etc.) exist in the data without catalog attribute entries. Flagged by audit, low risk.
- **No OAuth** — the MCP endpoint uses a single bearer token. claude.ai web Connectors require OAuth 2.1, so they can't currently plug in. Claude Code / Desktop and other scripted MCP clients work fine.
- **No Lantmäteriet data** — would add nationwide topographic context. Intentionally skipped for disk and scope.

