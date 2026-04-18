"""Probe all downloaded data sources and emit metadata.json per source.

Run from repo root: python3 scripts/probe.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

SCB_TABLES = {
    # TAB id -> (section, title, period)
    "TAB6680": ("Befolkningens arbetsmarknadsstatus (BAS)", "Arbetsmarknadsstatus efter bostadens belägenhet, region (DeSO/RegSO), kön och ålder", "2020-2024"),
    "TAB6681": ("Befolkningens arbetsmarknadsstatus (BAS)", "Sysselsatta 15–74 år efter bostadens belägenhet, region (DeSO/RegSO), kön och näringsgren (SNI 2007)", "2020-2024"),
    "TAB6682": ("Befolkningens arbetsmarknadsstatus (BAS)", "Sysselsatta 15–74 år efter bostadens belägenhet, region (DeSO/RegSO), kön och sektor", "2020-2024"),
    "TAB5956": ("Befolkningens utbildning", "Befolkning 25–64 år efter region och utbildningsnivå", "2015-2023"),
    "TAB6534": ("Befolkningens utbildning", "Befolkning 25–65 år efter region och utbildningsnivå", "2024"),
    "TAB6574": ("Befolkningsstatistik", "Folkmängden efter region, ålder och kön", "Yearly"),
    "TAB6571": ("Befolkningsstatistik", "Folkmängden efter region, utländsk/svensk bakgrund och kön", "Yearly"),
    "TAB6570": ("Befolkningsstatistik", "Folkmängden efter region, civilstånd och kön", "Yearly"),
    "TAB6572": ("Befolkningsstatistik", "Folkmängden efter region, födelseregion och kön", "Yearly"),
    "TAB6569": ("Befolkningsstatistik", "Folkmängden efter region, medborgarskap och kön", "Yearly"),
    "TAB6568": ("Befolkningsstatistik", "Antal hushåll efter region, hushållstyp", "Yearly"),
    "TAB6258": ("Bostadsbestånd", "Antal lägenheter efter region (DeSO 2018/RegSO 2020) och upplåtelseform", "2015-2023"),
    "TAB6638": ("Bostadsbestånd", "Antal lägenheter efter region (DeSO/RegSO 2025) och upplåtelseform", "2024"),
    "TAB6091": ("Fordonsstatistik", "Personbilar efter status och region (not updated)", "2015-2023"),
    "TAB6589": ("Fordonsstatistik", "Personbilar efter status och region", "2024"),
    "TAB6679": ("Hushållens ekonomi", "Andel av befolkningen per inkomstklass efter region, inkomstslag och kön", "Yearly"),
    "TAB6685": ("Hushållens ekonomi", "Låg respektive hög ekonomisk standard efter region och ålder", "Yearly"),
    "TAB6684": ("Hushållens ekonomi", "Ekonomisk standard, andel av befolkningen per inkomstklass efter region", "Yearly"),
    "TAB6683": ("Hushållens ekonomi", "Inkomststruktur nettoinkomst efter region och kön", "Yearly"),
    "TAB6065": ("Hushållens boende", "Antal personer efter region och hustyp", "Yearly"),
    "TAB6253": ("Hushållens boende", "Antal personer efter region och upplåtelseform", "Yearly"),
    "TAB660": ("Registerbaserad arbetsmarknadsstatistik (RAMS)", "Förvärvsarbetande nattbefolkning 16+ år efter region och bransch (SNI07)", "2018"),
    "TAB682": ("Registerbaserad arbetsmarknadsstatistik (RAMS)", "Förvärvsarbetande nattbefolkning 16+ år efter region, sektor och kön", "2018"),
    "TAB683": ("Registerbaserad arbetsmarknadsstatistik (RAMS)", "Befolkningen 16–64 år efter region, sysselsättning och kön", "2018"),
    "TAB5880": ("Registerbaserad arbetsmarknadsstatistik (RAMS)", "Befolkningen 16–64 år efter region, sysselsättning och kön — new time series", "2019+"),
    "TAB5842": ("Registerbaserad arbetsmarknadsstatistik (RAMS)", "Förvärvsarbetande nattbefolkning 16–74 år efter region och bransch (SNI07) — new time series", "2019+"),
    "TAB5843": ("Registerbaserad arbetsmarknadsstatistik (RAMS)", "Förvärvsarbetande nattbefolkning 16–74 år efter region, sektor och kön — new time series", "2019+"),
    "TAB6420": ("Miljö – Land- och vattenarealer", "Land- och vattenareal per den 1 januari efter region och arealtyp", "2025"),
    "TAB6620": ("Miljö – Bebyggelsestruktur och bostäder", "Bostadsbyggnader efter region och byggnadstyp", "2010-2024"),
    "TAB6540": ("Miljö – Bebyggelsestruktur och bostäder", "Bostadsbebyggelsens ålder efter region", "2010-2024"),
    "TAB6621": ("Miljö – Bebyggelsestruktur och bostäder", "Byggnader, antal och markyta efter region och byggnadstyp", "2010-2024"),
}

SCB_BULK_URL = "https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/{tab}_sv.zip"

# Per-section Px table links (from sources.md)
SCB_PXWEB = {
    "TAB6680": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0210__AM0210G/ArRegDesoStatusN/",
    "TAB6681": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0210__AM0210G/ArRegDesoSNI2007N/",
    "TAB6682": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0210__AM0210G/ArRegDesoSektorN/",
    "TAB5956": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__UF__UF0506__UF0506D/UtbSUNBefDesoRegso/",
    "TAB6534": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__UF__UF0506__UF0506D/UtbSUNBefDesoRegsoN/",
    "TAB6574": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__BE__BE0101__BE0101Y/FolkmDesoAldKon/",
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
    "TAB6683": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0110__HE0110I/Tab2InkDesoRegso/",
    "TAB6065": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0111__HE0111YDeSo/HushallT32Deso/",
    "TAB6253": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__HE__HE0111__HE0111YDeSo/HushallT33Deso/",
    "TAB660": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoBra/",
    "TAB682": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoSekt/",
    "TAB683": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoSyss/",
    "TAB5880": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/BefDeSoSyssN/",
    "TAB5842": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoBraN/",
    "TAB5843": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__AM__AM0207__AM0207I/NattDeSoSektN/",
    "TAB6420": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__MI__MI0802/Areal2025/",
    "TAB6620": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__MI__MI0803__MI0803B/Bostadsbyggnad3/",
    "TAB6540": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__MI__MI0803__MI0803B/BostadsbyggnadAlder3/",
    "TAB6621": "https://www.statistikdatabasen.scb.se/pxweb/sv/ssd/START__MI__MI0803__MI0803B/MarkanvByggnadLnKn3/",
}


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(chunk), b""):
            h.update(buf)
    return h.hexdigest()


def probe_csv(csv_path: Path, sample_rows: int = 5) -> dict:
    size = csv_path.stat().st_size
    # SCB bulk CSVs are Windows-1252 / cp1252, semicolon-delimited
    with csv_path.open("r", encoding="cp1252", newline="") as f:
        # sniff delimiter
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            delim = dialect.delimiter
        except csv.Error:
            delim = ";"
        reader = csv.reader(f, delimiter=delim)
        header = next(reader)
        samples: list[list[str]] = []
        row_count = 0
        uniques: list[set[str]] = [set() for _ in header]
        for row in reader:
            row_count += 1
            if len(samples) < sample_rows:
                samples.append(row)
            for i, v in enumerate(row[: len(header)]):
                if len(uniques[i]) < 12:
                    uniques[i].add(v)
    return {
        "path": str(csv_path.relative_to(DATA)),
        "size_bytes": size,
        "encoding": "windows-1252",
        "delimiter": delim,
        "row_count": row_count,
        "columns": [
            {
                "name": name,
                "sample_values": sorted(uniques[i])[:10],
                "distinct_sample_capped_at": 12,
            }
            for i, name in enumerate(header)
        ],
        "sample_rows": samples,
    }


def ogrinfo_json(path: Path, layer: str | None = None) -> dict:
    cmd = ["ogrinfo", "-json", "-so"]
    if layer is None:
        cmd += [str(path)]
    else:
        cmd += [str(path), layer]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(out.stdout)


def probe_geopackage_or_shp(path: Path) -> dict:
    top = ogrinfo_json(path)
    layers_out = []
    for ldesc in top.get("layers", []):
        name = ldesc["name"]
        ldetail = ogrinfo_json(path, name)
        layer_full = ldetail["layers"][0]
        fields = [
            {"name": f["name"], "type": f.get("type"), "width": f.get("width"), "precision": f.get("precision")}
            for f in layer_full.get("fields", [])
        ]
        geom_fields = layer_full.get("geometryFields", [])
        geom = geom_fields[0] if geom_fields else {}
        layers_out.append(
            {
                "name": name,
                "feature_count": layer_full.get("featureCount"),
                "geometry_type": geom.get("type"),
                "crs": {
                    "auth": geom.get("coordinateSystem", {}).get("projjson", {}).get("id", {}).get("authority"),
                    "code": geom.get("coordinateSystem", {}).get("projjson", {}).get("id", {}).get("code"),
                    "name": geom.get("coordinateSystem", {}).get("projjson", {}).get("name"),
                    "wkt": geom.get("coordinateSystem", {}).get("wkt"),
                },
                "extent": geom.get("extent"),
                "fields": fields,
            }
        )
    return {"path": str(path.relative_to(DATA)), "size_bytes": path.stat().st_size, "layers": layers_out}


def build_scb_metadata() -> dict:
    base = DATA / "scb_deso"
    geographic = []
    for gpkg in sorted((base / "geographic").glob("*.gpkg")):
        entry = probe_geopackage_or_shp(gpkg)
        entry["sha256"] = sha256(gpkg)
        entry["source_url"] = (
            f"https://geodata.scb.se/geoserver/stat/wfs?service=WFS&REQUEST=GetFeature&version=1.1.0"
            f"&TYPENAMES=stat:{gpkg.stem}&outputFormat=geopackage"
        )
        geographic.append(entry)

    tables = []
    for tab_dir in sorted((base / "tables").iterdir()):
        if not tab_dir.is_dir():
            continue
        tab_id = tab_dir.name
        meta = SCB_TABLES.get(tab_id, ("", "", ""))
        files = []
        for p in sorted(tab_dir.iterdir()):
            if p.is_file():
                probe = probe_csv(p) if p.suffix.lower() == ".csv" else {
                    "path": str(p.relative_to(DATA)),
                    "size_bytes": p.stat().st_size,
                }
                probe["sha256"] = sha256(p)
                files.append(probe)
        tables.append(
            {
                "tab_id": tab_id,
                "section": meta[0],
                "title": meta[1],
                "period": meta[2],
                "pxweb_url": SCB_PXWEB.get(tab_id),
                "bulk_zip_url": SCB_BULK_URL.format(tab=tab_id),
                "files": files,
            }
        )

    return {
        "source_key": "scb_deso",
        "title": "SCB DeSO — Geographic areas and bulk statistical tables",
        "publisher": "Statistikmyndigheten SCB (Statistics Sweden)",
        "license": {
            "name": "Creative Commons CC0 1.0",
            "url": "https://creativecommons.org/publicdomain/zero/1.0/",
        },
        "landing_pages": {
            "deso_overview": "https://www.scb.se/vara-tjanster/oppna-data/oppna-geodata/oppna-geodata-for-deso---demografiska-statistikomraden/",
            "deso_tables_index": "https://www.scb.se/hitta-statistik/regional-statistik-och-kartor/regionala-indelningar/demografiska-statistikomraden-deso/deso-tabellerna-i-ssd--information-och-instruktioner/",
        },
        "captured": "2026-04-17",
        "deso_versions": {
            "DeSO_2018": "Older time series; not back-revised.",
            "DeSO_2025": "Current small-area demographic grid (~6,160 areas).",
        },
        "encoding_note": "All bulk CSVs are semicolon-separated, Windows-1252 encoded.",
        "structure": {
            "geographic/": "DeSO polygon geopackages (2018 and 2025) fetched via WFS.",
            "tables/TAB<id>/": "One folder per PxWeb table containing the raw CSV from the SCB bulk endpoint.",
        },
        "geographic": geographic,
        "tables": tables,
    }


def build_sbk_metadata() -> dict:
    base = DATA / "stockholm_sbk"
    root = base / "Stadskarta_hela_Stockholm"
    shp_dir = root / "Stockholm_Utskr-Stadskarta-Standard_shp"

    layers = []
    for shp in sorted(shp_dir.glob("*.shp")):
        entry = probe_geopackage_or_shp(shp)
        entry["sha256"] = sha256(shp)
        layers.append(entry)

    other_files = []
    for p in sorted(root.iterdir()):
        if p.is_file():
            other_files.append(
                {
                    "path": str(p.relative_to(DATA)),
                    "size_bytes": p.stat().st_size,
                    "sha256": sha256(p),
                }
            )

    return {
        "source_key": "stockholm_sbk",
        "title": "Stockholms Stads SBK Stadskarta (1:4000–1:8000)",
        "publisher": "Stockholms stad — Stadsbyggnadskontoret (SBK)",
        "license": {
            "name": "Creative Commons CC0 1.0 (open data version)",
            "url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "note": "Open data version excludes 'registerenhet' (property units).",
        },
        "landing_pages": {
            "metadata": "https://dataportalen.stockholm.se/dataportalen/GetMetaDataById?id=0eed76ad-7a89-4da1-9766-e9fcf7b31789",
            "product_specification": "https://dataportalen.stockholm.se/dataportalen/Data/Stadsbyggnadskontoret/Allmän_produktspecifikation_Stadskarta.pdf",
        },
        "download_url": "https://dataportalen.stockholm.se/dataportalen/Data/Stadsbyggnadskontoret/Stadskarta_hela_Stockholm.zip",
        "captured": "2026-04-17",
        "coordinate_reference_system": {
            "epsg": 3011,
            "name": "SWEREF 99 18 00 / Stockholm local projection",
            "note": "Metadata states EPSG:3011; authoritative CRS is read from the .prj files of each shapefile.",
        },
        "scale_range": "1:4000 – 1:8000",
        "spatial_accuracy_meters": 10,
        "geographic_coverage": {
            "municipality": "Stockholm",
            "bbox_wgs84": {"west": 17.7605, "east": 18.2011, "south": 59.2272, "north": 59.4402},
        },
        "update_frequency": "Continuous",
        "last_revised": "2025-02-07",
        "last_published": "2026-02-18",
        "structure": {
            "Stadskarta_hela_Stockholm/": "Root folder of the extracted zip.",
            "Stadskarta_hela_Stockholm/Metadata_Stockholm_Utskr-Stadskarta-Standard.xlsx": "Publisher-supplied attribute metadata spreadsheet.",
            "Stadskarta_hela_Stockholm/Stockholm_Utskr-Stadskarta-Standard_shp/": "Esri Shapefile set, one shapefile per thematic layer (see 'layers').",
        },
        "other_files": other_files,
        "layers": layers,
    }


def main() -> None:
    scb = build_scb_metadata()
    (DATA / "scb_deso" / "metadata.json").write_text(
        json.dumps(scb, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("wrote data/scb_deso/metadata.json")

    sbk = build_sbk_metadata()
    (DATA / "stockholm_sbk" / "metadata.json").write_text(
        json.dumps(sbk, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("wrote data/stockholm_sbk/metadata.json")


if __name__ == "__main__":
    main()
