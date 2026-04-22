# Geodata MCP

**A municipal geodata stack designed for LLM-driven analysis, with enforced
provenance and schema normalization.** Stockholm Stadsbyggnadskontorets city
map, SCB demographic statistical areas (DeSO 2018 + 2025), SCB's DeSO-keyed
statistical tables, and OpenStreetMap addresses, all pre-joined to
canonical keys and exposed to any MCP client through a session-scoped
DuckDB spatial backend. 66 datasets, one viewer, 12 tools shaped for how
LLMs actually reason.

- **Live:** `https://geo.benjaminhenriksson.com`
- **MCP endpoint:** `POST /mcp` (streamable HTTP). OAuth 2.1 + PKCE via
  invite code for web custom-connector flows (claude.ai, ChatGPT,
  Gemini, etc.); legacy shared bearer for CLI clients (Claude Code,
  Claude Desktop, scripted MCP clients).
- **Viewer:** MapLibre GL JS, dark Carto basemap, click-to-inspect popups,
  per-layer toggles, auto-refresh on session state changes, thematic
  styling via `show(..., style=...)`.
- **License of underlying data:** CC0 1.0 (SCB + SBK Stadskarta open-data
  variant) + ODbL 1.0 (OpenStreetMap, "© OpenStreetMap contributors"
  attribution required on derived exports) + CC BY 4.0 (Lantmäteriet,
  not currently ingested).
- **Code license:** GNU Affero General Public License v3.0 or later
  (see `LICENSE`). The AGPL's network-use clause applies: running a
  modified version of the server as a public service obliges you to
  offer the modified source to its users.

---

## What it does

12 tools. All carry MCP `toolAnnotations` (`readOnlyHint` /
`destructiveHint=false`) so MCP clients (claude.ai, ChatGPT, Gemini,
Qwen, Claude Desktop / Code, and the rest) can auto-approve safe
calls without per-action permission prompts. Five of them take an
`op: Literal[...]` enum that picks the sub-operation; the valid
values are visible in the tool schema.

| Tool | What it does |
|---|---|
| `catalog` | Fuzzy-search the 65-dataset catalog; pass `id` for full attribute schema |
| `geocode` | Forward (name → coords), reverse (coords → admin area), or bbox (name → bbox) |
| `load` | Pull 1..N catalog datasets, or inject LLM-provided rows (`source` mandatory) |
| `execute_sql` | Read-only DuckDB + Spatial SQL, sqlglot-validated, 30 s timeout |
| `derive` | New layer from existing: `filter`, `top_n`, `clip`, `intersect`, `select_by_location`, `buffer`, `centroid`, `dissolve`, `convex_hull` |
| `edit_field` | Mutate columns in place: `add`, `update`, `drop`, `classify`, `annotate`. Reversible inside a checkpoint |
| `inspect` | `layers` (inventory), `rows` (sample, ≤200), `batch` (cursor-paginate), `at` (spatial "what's here", ≤500 points) |
| `layer` | Visibility + lifecycle: `show`, `hide`, `rename`, `drop`, `set_notes` |
| `export` | Data-only: single or multi layer → gpkg/geojson/csv/parquet; optional citation bundle |
| `render_map` | Server-rendered styled PNG with Carto Positron basemap underlay |
| `sources` | Walks the provenance chain → markdown citations |
| `checkpoint` | `create` a named savepoint, `rollback` to it, or `commit` it. Scoped + concurrent |

Full tool reference: **[`docs/tools.md`](docs/tools.md)**, plus the
published docs at [`geo.benjaminhenriksson.com/docs`](https://geo.benjaminhenriksson.com/docs)
(design rationale, architecture, data model, sessions, viewer,
provenance, rendering, roadmap). The server also publishes top-level
`instructions` at connection time, a ~6 KB workflow primer the model
(Claude, ChatGPT, Gemini, Qwen, etc.) reads before the first tool call.

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

# 3. OSM addresses (optional but recommended — needed for reliable
#    composite-address geocoding). Downloads Geofabrik's Sweden extract
#    (~800 MB one-time) and extracts ~131 k structured addresses for
#    Stockholm kommun. Uses ~1.5 GB RAM peak; run under a 2 GB cap on
#    modest hardware:
#       systemd-run --scope -p MemoryMax=2G -p AllowedCPUs=1 \
#         python scripts/fetch_osm.py
#    Output: data/normalized/osm/addresses.parquet (4 MB). Under ODbL —
#    attribute "© OpenStreetMap contributors" in any derived export.
```

Expected output layout and sizes are documented in
[`DATA_SUMMARY.md`](DATA_SUMMARY.md). The Phase-0 exploration scripts
(`scripts/deso_vs_stockholm.py`, `filter_estimate.py`, `overlap_check.py`)
are retained as one-shots that informed the filtering decisions.

---

## Quick start for MCP clients

Two connection paths: an OAuth flow for web clients, and a bearer
token for CLI / desktop clients that haven't shipped OAuth 2.1 + PKCE
support yet.

### 1. Web custom-connector flow (preferred; claude.ai, ChatGPT, Gemini, etc.)

Open your AI app's connector settings, choose Add custom connector
(naming varies; it's "Custom connector" in claude.ai, similar in
ChatGPT and Gemini), and paste:

```
https://geo.benjaminhenriksson.com/mcp
```

You'll be redirected to a consent form that shows the callback host
and asks for an invite code. Type the code (shared out-of-band) and
you're in. The same endpoint URL works for every MCP-capable web
client. The protocol is identical, only the connector dialog UI
differs.

Get the invite code from the host operator; it lives in
`/etc/credstore/geodata-mcp.invite` (root-readable only).

### 2. Claude Code CLI (legacy shared bearer, example)

```bash
TOKEN=$(ssh geo-vps 'sudo cat /etc/credstore/geodata-mcp.token')
claude mcp add --transport http geodata \
  https://geo.benjaminhenriksson.com/mcp \
  --header "Authorization: Bearer $TOKEN"
```

Restart the client. The 12 tools appear automatically. The same
bearer-on-`/mcp` pattern works for any CLI-style MCP client (Codex,
custom scripts, in-house tools); only the `add` command syntax
differs.

### 3. Desktop apps via the `mcp-remote` shim (Claude Desktop example)

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(or the equivalent config file for your desktop client):

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
geocode(op="forward", name="Tekniska nämndhuset")        # → (152699, 6579781)
load(op="catalog", dataset_ids=["deso_2025", "scb_income_structure"])
execute_sql(sql="""
  WITH here AS (
    SELECT desokod FROM deso_2025
    WHERE ST_Contains(geom, ST_Point(152699, 6579781))
  )
  SELECT år, value AS mean_tkr
  FROM scb_income_structure i JOIN here h ON i.desokod_2025 = h.desokod
  WHERE tabellinnehåll='Medelvärde för samtliga, tkr' AND kön='totalt'
    AND inkomstkomponent='nettoinkomst' AND value IS NOT NULL
  ORDER BY år DESC LIMIT 3
""")

sources(layer="deso_2025")   # → markdown citation block
```

Actual answer: this DeSO's mean net income in 2024 was **632 tkr** (Stockholm
kommun average 453 tkr; national 358 tkr), ~83rd percentile among 544 Stockholm
DeSOs. See `docs/tools.md` for the full reference.

### "Show me all buildings in Södermalm"

```python
load(op="catalog", dataset_ids=["sbk_admin_polygons"], layer_name="admin")
derive(op="filter", layer="admin",
       where="KATEGORI='Stadsdel' AND NAMN='SÖDERMALM'",
       result_name="sodermalm")
load(op="catalog", dataset_ids=["sbk_buildings"],
     intersect_layer="sodermalm", layer_name="bldg_sodermalm")
layer(op="show", layers=["sodermalm", "bldg_sodermalm"])
```

3,173 buildings clipped by the actual Stadsdel polygon.

### "Where do my favorite cafés sit in the DeSO grid?"

```python
load(op="inline", layer_name="favorite_cafes",
     data=[{"name": "Drop Coffee", "rating": 4.6, "geom_wkt": "POINT(18.0658 59.3177)"},
           {"name": "Café Pascal", "rating": 4.4, "geom_wkt": "POINT(18.0525 59.3427)"}],
     source="Personal visits 2026-04", geometry_column="geom_wkt")
execute_sql(sql="""
  SELECT c.name, c.rating, d.desokod
  FROM favorite_cafes c JOIN deso_2025 d ON ST_Contains(d.geom, c.geom)
""")
export(layers="favorite_cafes", format="gpkg", cite=True)
```

---

## Datasets (65 total)

- **DeSO 2018** + **DeSO 2025** polygons (Stockholm), EPSG:3011
- **DeSO 2018↔2025 historical changes** (1,234 rows): SCB's official migration mapping
- **DeSO↔RegSO connection table** (6,160 rows)
- **30 SBK Stadskarta layers**: buildings, addresses, place names, administrative areas, streets, water, contours, etc. (Stockholm kommun, EPSG:3011)
- **31 SCB statistical tables** keyed by DeSO/RegSO/kommun/Riket: population, households, income, employment, housing stock, buildings-by-age, land use, cars (Stockholm DeSOs + Stockholm RegSOs + country aggregate)

Run `catalog()` to list them all. Full source notes in
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

Full deployment details: **[`docs/_deployment.md`](docs/_deployment.md)** (private; VPS specifics).

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

# run locally (stdio; plug into any MCP client config; example below
# uses Claude Code, but the same stdio entry point works for ChatGPT
# Desktop, Codex, in-house MCP clients, etc.)
uv run python -m geodata_mcp

# run HTTP (same as production)
uv run python -m geodata_mcp --http --host 127.0.0.1 --port 8765
```

### Audits

Two CI-ready audits live in `scripts/`:

- `catalog_audit.py`: every catalog `sample_values` entry must appear in the data, feature counts must match, no undeclared columns without explanation.
- `cross_ref_audit.py`: every DeSO `region` code across SCB tables must resolve to a polygon in the 2018 grid, 2025 grid, or SCB's historical-changes mapping.

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
│   ├── server.py                — FastMCP + 12 @mcp.tool wrappers + main entry
│   ├── http_app.py              — Starlette routes + middleware (built only with --http)
│   ├── catalog.py               — rapidfuzz-backed catalog search
│   ├── session.py               — per-connection sessions, GC, persistence, SQL audit
│   ├── loader.py                — read normalized data → DuckDB
│   ├── operations/              — split package: sql/spatial/query/layers/fields/export/checkpoint/inspect
│   ├── render.py                — server-side PNG rendering (matplotlib)
│   ├── oauth.py                 — invite-gated OAuth 2.1 + PKCE
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

- **Coverage is Stockholm kommun.** Everything is filtered to kommunkod `0180`. Expanding is mostly a data-ingest problem, not a tool-design problem.
- **Street + house-number geocoding** pairs via 250 m spatial join between the street-label point and the nearest address-number point. Some pairs are off-by-one-building.
- **SCB privacy suppression.** Small-population DeSOs have NULL values in statistical tables. Catalog descriptions warn about this; queries must `WHERE value IS NOT NULL` to skip them.
- **38 catalog warnings.** SBK cartographic columns (TEXTFONT, TEXT_ANGLE, etc.) exist in the data without catalog attribute entries. Flagged by audit, low risk.
- **CLI clients still use a shared bearer.** The legacy bearer flow is fine for any scripted/CLI MCP client (Claude Code, Claude Desktop, Codex, custom scripts). Web custom-connector flows (claude.ai, ChatGPT, Gemini, etc.) use the OAuth 2.1 + PKCE path described above. Either flow gates on the same invite code.
- **No Lantmäteriet data.** Would add nationwide topographic context. Intentionally skipped for disk and scope.

