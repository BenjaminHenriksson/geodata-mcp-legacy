"""Generate catalog.json entries for every normalized file that isn't yet in
catalog.json. Produces a JSON array that the operator can merge.

For SBK GPKGs: probe top-N values per categorical column and assemble a bilingual
description from a small theme lookup.
For SCB parquets: use the SCB_TABLES metadata (title, period, section) baked into
the Phase 0 probe script.

Run: uv run python scripts/catalog_autogen.py > /tmp/new_entries.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog.json"

# Swedish/English themes per SBK filename stem.  (description_sv, description_en, keywords_sv, keywords_en)
SBK_THEMES = {
    "0_line": ("Höjdkurvor (2 m ekvidistans) som linjer, för terrängrelief på stadskartan.",
                "Elevation contour lines (2 m spacing) used to render terrain relief on the city map.",
                ["höjdkurva", "topografi", "terräng"], ["contour", "elevation", "terrain"]),
    "Adm_line": ("Administrativa gränslinjer: kommungräns, stadsdelsgräns, distriktsgräns, kvartersnamnsgräns.",
                  "Administrative boundary lines: kommun, stadsdel, distrikt, block-name.",
                  ["gräns", "administrativ"], ["boundary", "administrative"]),
    "Anlaeggning_area": ("Anläggningar som polygoner (fritidsanläggning, begravningsplats, hamn m.m.).",
                         "Facilities and constructed areas as polygons (sports facilities, cemeteries, harbours, etc.).",
                         ["anläggning"], ["facility", "installation"]),
    "Anlaeggning_line": ("Anläggningar som linjer (pir, bro, stängsel, staket).",
                         "Facilities and linear constructions (piers, bridges, fences).",
                         ["anläggning"], ["facility"]),
    "Anlaeggning_point": ("Anläggningar som punkter (brunnslock, transformator, signaler).",
                          "Facilities and point features (manholes, transformers, signals).",
                          ["anläggning"], ["facility", "point"]),
    "Byggnad_line": ("Byggnadskonturer som linjer (komplement till Byggnad_area-polygonerna).",
                     "Building outlines as linework (complements the Byggnad_area polygons).",
                     ["byggnad", "kontur"], ["building", "outline"]),
    "FastighetText_point": ("Fastighetsetiketter (beteckningar som 'Stora Essingen 2:1') som punkter.",
                            "Property-designation labels (e.g. 'Stora Essingen 2:1') as points.",
                            ["fastighet", "etikett"], ["property", "parcel", "label"]),
    "Fastighet_area": ("Fastighetspolygoner (exklusive registerenhet i öppen data-versionen).",
                       "Property polygons (register-unit attributes excluded in open-data release).",
                       ["fastighet"], ["property", "parcel", "cadastral"]),
    "Fastighet_line": ("Fastighetsgränser som linjer.",
                       "Property boundary lines.",
                       ["fastighet", "gräns"], ["property", "parcel", "boundary"]),
    "Infra_area": ("Infrastruktur som polygoner (byggnadsnära mark, parkering, spårområden m.m.).",
                   "Infrastructure polygons (building-adjacent ground, parking, rail, etc.).",
                   ["infrastruktur"], ["infrastructure"]),
    "Infra_line": ("Infrastruktur som linjer (järnväg, spårväg, kraftledning, kraftlinje).",
                   "Infrastructure lines (railway, tram, power line).",
                   ["infrastruktur", "järnväg", "spårväg"], ["infrastructure", "railway", "tram"]),
    "Infra_point": ("Infrastrukturpunkter (stolpe, brunn, lyktstolpe).",
                    "Infrastructure points (masts, wells, lamp posts).",
                    ["infrastruktur"], ["infrastructure", "point"]),
    "Koloni_area": ("Koloniområden (kolonilotter) som polygoner.",
                    "Allotment-garden areas as polygons.",
                    ["koloni", "odlingslott"], ["allotment", "garden"]),
    "Koloni_line": ("Kolonigränser som linjer.",
                    "Allotment boundary lines.",
                    ["koloni"], ["allotment"]),
    "Land_area": ("Landyta (storsammanhängande markyta utan detaljindelning).",
                  "Land surface (continuous land area without fine-grained subdivision).",
                  ["mark", "land"], ["land", "surface"]),
    "Mark_area": ("Markslag som polygoner (öppen mark, skog, berg i dagen, impediment).",
                  "Land-cover polygons (open land, forest, bare rock, non-productive).",
                  ["markslag", "mark"], ["land cover", "vegetation"]),
    "Sankmark_area": ("Sankmark (våtmark, myr) som polygoner.",
                      "Wetland / marsh polygons.",
                      ["sankmark", "våtmark"], ["wetland", "marsh"]),
    "Sankmark_line": ("Sankmarksgränser som linjer.",
                      "Wetland boundary lines.",
                      ["sankmark"], ["wetland"]),
    "TeckenText_point": ("Teckenförklaring — etikettpunkter som styr renderingen av kartsymboler.",
                         "Legend — label points that control rendering of map symbols.",
                         ["teckenförklaring", "symbol"], ["legend", "symbol"]),
    "TextKurvad_line": ("Kurvtexter — osynliga linjer som text följer (etikettbärare).",
                         "Curved text guides — invisible lines that labels follow.",
                         ["text", "kurva"], ["text", "label"]),
    "Trafik_area": ("Trafikytor som polygoner (körbana, trottoar, cykelbana, trafikyta).",
                    "Traffic area polygons (carriageway, sidewalk, bike path, mixed traffic area).",
                    ["trafik", "väg"], ["traffic", "road surface"]),
    "Trafik_line": ("Trafiklinjer (väglinjer, körfält, cykelbanor, spår).",
                    "Traffic lines (road centerlines, lanes, bike paths, rail).",
                    ["trafik", "väg"], ["traffic", "road"]),
    "Vaegutbredning_area": ("Vägutbredning som polygoner (heltäckande vägyta inkl. kantremsor).",
                             "Full road-surface extent polygons (including verges).",
                             ["väg", "utbredning"], ["road", "surface extent"]),
    "Vaegutbredning_line": ("Vägutbredning som linjer (kantlinjer, körfält).",
                             "Road-surface extent lines (kerb lines, lane edges).",
                             ["väg"], ["road"]),
    "Vatten_area": ("Vattenpolygoner (hav, sjö, vattendrag, bassäng).",
                    "Water polygons (sea, lake, watercourse, basin).",
                    ["vatten", "hav", "sjö"], ["water", "sea", "lake"]),
    "Vatten_line": ("Vattenlinjer (strandlinje, vattendrag).",
                    "Water lines (shorelines, watercourses).",
                    ["vatten", "strandlinje"], ["water", "shoreline"]),
}

# SCB bulk-table metadata baked in (mirrors scripts/probe.py)
SCB_TABLES = {
    "TAB6680": ("Befolkningens arbetsmarknadsstatus (BAS)", "Arbetsmarknadsstatus efter bostadens belägenhet, region (DeSO/RegSO), kön och ålder", "2020-2024",
                "Labour-market status by residence, region (DeSO/RegSO), sex, age", "Employment status"),
    "TAB6681": ("Befolkningens arbetsmarknadsstatus (BAS)", "Sysselsatta 15–74 år efter bostadens belägenhet, region (DeSO/RegSO), kön och näringsgren (SNI 2007)", "2020-2024",
                "Employed 15–74 by residence region, sex, industry SNI 2007", "Employed by industry"),
    "TAB6682": ("Befolkningens arbetsmarknadsstatus (BAS)", "Sysselsatta 15–74 år efter bostadens belägenhet, region (DeSO/RegSO), kön och sektor", "2020-2024",
                "Employed 15–74 by region, sex, sector", "Employed by sector"),
    "TAB5956": ("Befolkningens utbildning", "Befolkning 25–64 år efter region och utbildningsnivå", "2015-2023",
                "Population 25–64 by region and education level", "Education (legacy)"),
    "TAB6534": ("Befolkningens utbildning", "Befolkning 25–65 år efter region och utbildningsnivå", "2024",
                "Population 25–65 by region and education level", "Education (current)"),
    "TAB6571": ("Befolkningsstatistik", "Folkmängden efter region, utländsk/svensk bakgrund och kön", "Yearly",
                "Population by region, Swedish/foreign background, sex", "Population by background"),
    "TAB6570": ("Befolkningsstatistik", "Folkmängden efter region, civilstånd och kön", "Yearly",
                "Population by region, marital status, sex", "Population by marital status"),
    "TAB6572": ("Befolkningsstatistik", "Folkmängden efter region, födelseregion och kön", "Yearly",
                "Population by region, birth region, sex", "Population by birth region"),
    "TAB6569": ("Befolkningsstatistik", "Folkmängden efter region, medborgarskap och kön", "Yearly",
                "Population by region, citizenship, sex", "Population by citizenship"),
    "TAB6568": ("Befolkningsstatistik", "Antal hushåll efter region, hushållstyp", "Yearly",
                "Households by region and household type", "Households by type"),
    "TAB6258": ("Bostadsbestånd", "Antal lägenheter efter region (DeSO 2018/RegSO 2020) och upplåtelseform (not updated)", "2015-2023",
                "Dwellings by region and tenure (DeSO 2018 series, not updated)", "Dwellings by tenure (legacy)"),
    "TAB6638": ("Bostadsbestånd", "Antal lägenheter efter region (DeSO/RegSO 2025) och upplåtelseform", "2024",
                "Dwellings by region and tenure (DeSO 2025)", "Dwellings by tenure"),
    "TAB6091": ("Fordonsstatistik", "Personbilar efter status och region (not updated)", "2015-2023",
                "Passenger cars by status and region (legacy, not updated)", "Cars (legacy)"),
    "TAB6589": ("Fordonsstatistik", "Personbilar efter status och region", "2024",
                "Passenger cars by status and region", "Cars"),
    "TAB6679": ("Hushållens ekonomi", "Andel av befolkningen per inkomstklass efter region, inkomstslag och kön", "Yearly",
                "Population share per income class by region, component, sex", "Income distribution"),
    "TAB6685": ("Hushållens ekonomi", "Låg respektive hög ekonomisk standard efter region och ålder", "Yearly",
                "Low/high economic standard by region and age", "Economic standard"),
    "TAB6684": ("Hushållens ekonomi", "Ekonomisk standard, andel av befolkningen per inkomstklass efter region", "Yearly",
                "Economic standard — share per income class by region", "Economic standard (share)"),
    "TAB6065": ("Hushållens boende", "Antal personer efter region och hustyp", "Yearly",
                "Number of persons by region and dwelling type", "Persons by dwelling type"),
    "TAB6253": ("Hushållens boende", "Antal personer efter region och upplåtelseform", "Yearly",
                "Number of persons by region and tenure", "Persons by tenure"),
    "TAB660":  ("RAMS 2018", "Förvärvsarbetande nattbefolkning 16+ år efter region och bransch (SNI07)", "2018",
                "Gainfully employed night population 16+ by region and industry (legacy RAMS 2018)", "Employed by industry (2018)"),
    "TAB682":  ("RAMS 2018", "Förvärvsarbetande nattbefolkning 16+ år efter region, sektor och kön", "2018",
                "Gainfully employed night population 16+ by region, sector, sex (2018)", "Employed by sector (2018)"),
    "TAB683":  ("RAMS 2018", "Befolkningen 16–64 år efter region, sysselsättning och kön", "2018",
                "Population 16–64 by region, activity, sex (2018)", "Employment status (2018)"),
    "TAB5880": ("RAMS (new series)", "Befolkningen 16–64 år efter region, sysselsättning och kön", "2019-",
                "Population 16–64 by region, activity, sex (new RAMS series)", "Employment status (new)"),
    "TAB5842": ("RAMS (new series)", "Förvärvsarbetande nattbefolkning 16–74 år efter region och bransch (SNI07)", "2019-",
                "Gainfully employed 16–74 by region and industry (new RAMS)", "Employed by industry (new)"),
    "TAB5843": ("RAMS (new series)", "Förvärvsarbetande nattbefolkning 16–74 år efter region, sektor och kön", "2019-",
                "Gainfully employed 16–74 by region, sector, sex (new RAMS)", "Employed by sector (new)"),
    "TAB6420": ("Miljö — areal", "Land- och vattenareal per den 1 januari efter region och arealtyp", "2025",
                "Land and water area by region and area type", "Land + water area"),
    "TAB6620": ("Miljö — bebyggelse", "Bostadsbyggnader efter region och byggnadstyp", "2010-2024",
                "Residential buildings by region and type", "Residential buildings"),
    "TAB6540": ("Miljö — bebyggelse", "Bostadsbebyggelsens ålder efter region", "2010-2024",
                "Age of residential buildings by region", "Residential building age"),
    "TAB6621": ("Miljö — bebyggelse", "Byggnader, antal och markyta efter region och byggnadstyp", "2010-2024",
                "Buildings — count and ground area by region and type", "Buildings + ground area"),
}

SCB_PXWEB = {
    "TAB6680": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0210__AM0210G/ArRegDesoStatusN/",
    "TAB6681": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0210__AM0210G/ArRegDesoSNI2007N/",
    "TAB6682": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0210__AM0210G/ArRegDesoSektorN/",
    "TAB5956": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__UF__UF0506__UF0506D/UtbSUNBefDesoRegso/",
    "TAB6534": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__UF__UF0506__UF0506D/UtbSUNBefDesoRegsoN/",
    "TAB6571": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BE__BE0101__BE0101Y/FolkmDesoBakgrKon/",
    "TAB6570": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BE__BE0101__BE0101Y/FolkmDesoCivilKon/",
    "TAB6572": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BE__BE0101__BE0101Y/FolkmDesoLandKon/",
    "TAB6569": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BE__BE0101__BE0101Y/FolkmDesoMedKon/",
    "TAB6568": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BE__BE0101__BE0101Y/HushallDesoTyp/",
    "TAB6258": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BO__BO0104__BO0104X/BO0104T10N/",
    "TAB6638": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BO__BO0104__BO0104X/BO0104T01N2/",
    "TAB6091": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__TK__TK1001__TK1001Z/PersBilarDeso/",
    "TAB6589": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__TK__TK1001__TK1001Z/PersBilarDesoN/",
    "TAB6679": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0110__HE0110I/Tab1InkDesoRegso/",
    "TAB6685": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0110__HE0110I/Tab4InkDesoRegso/",
    "TAB6684": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0110__HE0110I/Tab3InkDesoRegso/",
    "TAB6065": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0111__HE0111YDeSo/HushallT32Deso/",
    "TAB6253": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0111__HE0111YDeSo/HushallT33Deso/",
    "TAB660":  "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoBra/",
    "TAB682":  "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoSekt/",
    "TAB683":  "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoSyss/",
    "TAB5880": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/BefDeSoSyssN/",
    "TAB5842": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoBraN/",
    "TAB5843": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoSektN/",
    "TAB6420": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__MI__MI0802/Areal2025/",
    "TAB6620": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__MI__MI0803__MI0803B/Bostadsbyggnad3/",
    "TAB6540": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__MI__MI0803__MI0803B/BostadsbyggnadAlder3/",
    "TAB6621": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__MI__MI0803__MI0803B/MarkanvByggnadLnKn3/",
}


def _top_values(con, src_expr, col, n=6):
    try:
        rows = con.execute(
            f'SELECT "{col}" FROM {src_expr} '
            f'WHERE "{col}" IS NOT NULL AND "{col}" <> \'\' '
            f'GROUP BY "{col}" ORDER BY COUNT(*) DESC LIMIT {n}'
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def _geom_type(con, src_expr, geom_col):
    try:
        rows = con.execute(
            f'SELECT ST_GeometryType("{geom_col}") FROM {src_expr} LIMIT 1'
        ).fetchone()
        t = rows[0] if rows else None
        # normalize: "POLYGON" → "Polygon" etc.
        if not t:
            return None
        simple = t.replace("MULTI", "").title().replace("Linestring", "LineString")
        return simple
    except Exception:
        return None


def generate_sbk_entries(con) -> list[dict]:
    existing_paths = {d["file_path"] for d in json.loads(CATALOG.read_text())["datasets"]}
    out: list[dict] = []
    for gpkg in sorted((ROOT / "data/normalized/sbk").glob("*.gpkg")):
        stem = gpkg.stem
        ds_id = f"sbk_{stem.lower().replace('.', '_')}"
        rel = str(gpkg.relative_to(ROOT))
        if rel in existing_paths:
            continue
        src = f"ST_Read('{gpkg}')"
        try:
            raw = con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()
            schema = [(r[0], r[1]) for r in raw]
            n = con.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0]
        except Exception as e:
            print(f"[warn] could not probe {gpkg.name}: {e}", file=sys.stderr)
            continue

        geom_col = next((c for c, t in schema if t.upper().startswith("GEOMETRY")), None)
        geom_type = _geom_type(con, src, geom_col) if geom_col else None
        theme = SBK_THEMES.get(stem,
                               (f"{stem} (SBK Stadskarta)", f"{stem} (SBK city map)", [], []))
        desc_sv, desc_en, kw_sv, kw_en = theme

        attrs = []
        for col_name, col_type in schema:
            if col_name == geom_col:
                continue
            samples = _top_values(con, src, col_name) if col_type.upper().startswith("VARCHAR") else []
            desc = {
                "GRUPP": ("Övergripande tematisk grupp", "Top-level thematic group"),
                "KATEGORI": ("Underkategori/typ", "Sub-category / type"),
                "KOMPONENT": ("Renderingskomponent (kartografi)", "Rendering component (cartographic)"),
                "RAMKLIPP": ("Om objektet är ramklippt", "Whether the feature is frame-clipped"),
                "NAMN": ("Namn (om finns)", "Name (if any)"),
                "DNR": ("Diarienummer", "Case number"),
                "TEXT": ("Renderad etiketttext", "Rendered label text"),
                "ADM_ID": ("Stabilt ID för administrativa polygoner", "Stable ID for admin polygons"),
            }.get(col_name, (f"{col_name} (SBK)", f"{col_name} (SBK)"))
            attrs.append({
                "name": col_name,
                "type": col_type.split("(")[0],
                "description_sv": desc[0],
                "description_en": desc[1],
                "sample_values": samples,
            })

        kind_suffix = {"area": "-polygoner", "line": "-linjer", "point": "-punkter"}
        kind_tag = ""
        if "_area" in stem: kind_tag = " (polygoner)"
        elif "_line" in stem: kind_tag = " (linjer)"
        elif "_point" in stem: kind_tag = " (punkter)"

        out.append({
            "id": ds_id,
            "name_sv": f"{stem.replace('_', ' ')} — SBK Stadskarta{kind_tag}",
            "name_en": f"{stem.replace('_', ' ')} — SBK city map{kind_tag}",
            "description_sv": desc_sv,
            "description_en": desc_en,
            "source_type": "geopackage",
            "file_path": str(gpkg.relative_to(ROOT)),
            "layer": stem,
            "crs_epsg": 3011,
            "geometry_type": geom_type,
            "feature_count": int(n),
            "coverage": "Stockholm kommun",
            "temporal": "Snapshot 2025-02-07",
            "keywords_sv": kw_sv + ["sbk", "stadskarta"],
            "keywords_en": kw_en + ["sbk", "city map"],
            "attributes": attrs,
            "publisher": "Stockholms stad — Stadsbyggnadskontoret (SBK)",
            "license": "CC0 1.0",
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "source_url": "https://dataportalen.stockholm.se/dataportalen/Data/Stadsbyggnadskontoret/Stadskarta_hela_Stockholm.zip",
            "retrieved": "2026-04-17",
        })
    return out


def generate_scb_entries(con) -> list[dict]:
    existing_paths = {d["file_path"] for d in json.loads(CATALOG.read_text())["datasets"]}
    out: list[dict] = []
    for pq in sorted((ROOT / "data/normalized/scb").glob("*.parquet")):
        tab = pq.stem
        rel = str(pq.relative_to(ROOT))
        if rel in existing_paths:
            continue
        meta = SCB_TABLES.get(tab)
        if not meta:
            print(f"[warn] no SCB metadata for {tab}; skipping", file=sys.stderr)
            continue
        section, title_sv, period, title_en, short_en = meta

        src = f"read_parquet('{pq}')"
        raw = con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()
        schema = [(r[0], r[1]) for r in raw]
        n = con.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0]

        # Top categorical values per column
        attrs = []
        for col_name, col_type in schema:
            samples = []
            if col_type.upper().startswith("VARCHAR"):
                samples = _top_values(con, src, col_name, n=8)
            elif col_type.upper().startswith("BIGINT") or col_type.upper().startswith("INTEGER"):
                # For year column give range min..max
                try:
                    row = con.execute(f'SELECT MIN("{col_name}"), MAX("{col_name}") FROM {src}').fetchone()
                    if row and row[0] is not None:
                        samples = [str(row[0]), str(row[1])]
                except Exception:
                    pass
            descs = {
                "region": ("DeSO-kod / RegSO-namn / '0180 Stockholm' / '00 Riket' — se scb_population_age_sex.",
                           "DeSO code / RegSO name / kommun / country — see scb_population_age_sex."),
                "kön": ("Kön: 'män', 'kvinnor', 'totalt'", "Sex: 'män', 'kvinnor', 'totalt'"),
                "år": ("Mätår", "Year"),
                "tabellinnehåll": ("Mätetal (mått + enhet). Inspektera sample_values.",
                                    "Measure (kind + unit). Inspect sample_values."),
            }
            d = descs.get(col_name, (f"{col_name} ({tab})", f"{col_name} ({tab})"))
            attrs.append({
                "name": col_name,
                "type": col_type.split("(")[0],
                "description_sv": d[0],
                "description_en": d[1],
                "sample_values": samples,
            })

        ds_id = f"scb_{tab.lower()}"
        out.append({
            "id": ds_id,
            "name_sv": f"{title_sv} (SCB {tab})",
            "name_en": f"{title_en} (SCB {tab})",
            "description_sv": f"{section}. {title_sv}. Stockholm-filtrerat (DeSO + RegSO + '00 Riket'). SCB sekretessmaskar värden i glesbefolkade DeSO — förvänta NULL.",
            "description_en": f"{section}. {title_en}. Stockholm-filtered (DeSO + RegSO + '00 Riket'). SCB suppresses values in sparsely populated DeSO — expect NULLs.",
            "source_type": "parquet",
            "file_path": str(pq.relative_to(ROOT)),
            "layer": None,
            "crs_epsg": None,
            "geometry_type": None,
            "feature_count": int(n),
            "coverage": "Stockholm DeSO + Stockholm RegSO + 00 Riket",
            "temporal": period,
            "keywords_sv": [section.lower(), "scb", tab.lower()],
            "keywords_en": [short_en.lower(), "scb", tab.lower()],
            "attributes": attrs,
            "publisher": "Statistikmyndigheten SCB",
            "license": "CC0 1.0",
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "source_url": SCB_PXWEB.get(tab, f"https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/{tab}_sv.zip"),
            "retrieved": "2026-04-17",
        })
    return out


def main() -> int:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    entries = generate_sbk_entries(con) + generate_scb_entries(con)
    print(json.dumps(entries, indent=2, ensure_ascii=False))
    print(f"\n# generated {len(entries)} entries", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
