# Data sources pulled

Snapshot of everything currently under `data/`. Captured **2026-04-17**.

See each source's own `README.md` and `metadata.json` for full per-file schema, CRS, feature counts, SHA-256 hashes, and refetch instructions.

## What was pulled

| Source | Path | Size | Records | License |
|---|---|---:|---|---|
| **SCB DeSO: Geographic areas** (2018 + 2025) | `data/scb_deso/geographic/` | 90 MB | 2 GeoPackages, 12,144 polygons total | CC0 1.0 |
| **SCB DeSO: Bulk statistical tables** | `data/scb_deso/tables/` | 8.4 GB | 31 CSVs, 121 M rows total | CC0 1.0 |
| **Stockholm SBK: Stadskarta (1:4000–1:8000)** | `data/stockholm_sbk/` | 469 MB | 30 Esri shapefiles + publisher metadata xlsx | CC0 1.0 (open-data variant) |

**Total on disk:** ~8.9 GB

## What was NOT pulled

- **Lantmäteriet, Karta 1:10 000 (CC BY 4.0).** Skipped by request. The FTP at `ftp://download-opendata.lantmateriet.se/Karta_1_10000_raster/` holds two variants (`Fastighetsindelning`, `Vagnamn`), ~82 top-level tiles each, with ~5 MB TIF tiles inside; a full download would be tens to hundreds of GB.

## Details

### `data/scb_deso/`

- **Publisher:** Statistikmyndigheten SCB (Statistics Sweden)
- **Captured via:**
  - WFS (GeoPackages): `https://geodata.scb.se/geoserver/stat/wfs?…&TYPENAMES=stat:DeSO_{2018,2025}&outputFormat=geopackage`
  - Bulk CSV zips: `https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB<id>_sv.zip`
- **Geographic layers:**
  - `DeSO_2018.gpkg`: 5,984 polygons, CRS **EPSG:3006 SWEREF99 TM**
  - `DeSO_2025.gpkg`: 6,160 polygons, CRS **EPSG:3006 SWEREF99 TM**
- **Tables:** 31 `TAB<id>/TAB<id>_sv.csv` files covering population, households, education, labor-market status (BAS + RAMS), vehicles, housing stock, income, environment/land-use, and buildings.
- **CSV format:** Comma-separated, fields double-quoted, **Windows-1252** (`cp1252`) encoded, `..` marks missing values. *Note: sources.md says semicolon, but the actual DeSO bulk feed emits commas.*

Full table catalogue (31 tables) and per-file column schema: `data/scb_deso/README.md` + `data/scb_deso/metadata.json`.

### `data/stockholm_sbk/`

- **Publisher:** Stockholms stad, Stadsbyggnadskontoret (SBK)
- **Captured via:** `https://dataportalen.stockholm.se/dataportalen/Data/Stadsbyggnadskontoret/Stadskarta_hela_Stockholm.zip`
- **Coverage:** Stockholm municipality, scale range **1:4000–1:8000**, nominal accuracy ~10 m
- **CRS:** EPSG:3011 (Stockholm-local Transverse Mercator, SWEREF 99 18 00), authoritative per shapefile's `.prj`
- **Contents:** 30 Esri shapefiles (one per thematic layer: buildings, properties, roads, addresses, administrative boundaries, water, wetlands, contours, etc.) plus a publisher-supplied attribute-metadata spreadsheet (`Metadata_Stockholm_Utskr-Stadskarta-Standard.xlsx`).

Full layer inventory and per-layer attribute schema: `data/stockholm_sbk/README.md` + `data/stockholm_sbk/metadata.json`.

## Repo layout

```
geodata-mcp/
├── sources.md               ← original source list (input to this pull)
├── DATA_SUMMARY.md          ← this file
├── scripts/
│   └── probe.py             ← regenerates metadata.json for both sources
└── data/
    ├── scb_deso/
    │   ├── README.md
    │   ├── metadata.json
    │   ├── geographic/
    │   │   ├── DeSO_2018.gpkg
    │   │   └── DeSO_2025.gpkg
    │   └── tables/
    │       ├── urls.txt
    │       └── TAB<id>/TAB<id>_sv.csv   × 31
    └── stockholm_sbk/
        ├── README.md
        ├── metadata.json
        └── Stadskarta_hela_Stockholm/
            ├── Metadata_Stockholm_Utskr-Stadskarta-Standard.xlsx
            └── Stockholm_Utskr-Stadskarta-Standard_shp/   (30 shapefiles)
```
