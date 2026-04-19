"""Phase 0 preprocessor: raw data -> data/normalized/.

Produces a canonical tree the MCP server reads exclusively:
 - data/normalized/sbk/<layer>.gpkg   (30 files, EPSG:3011 preserved)
 - data/normalized/deso/DeSO_{2018,2025}.gpkg  (kommunkod='0180', reprojected to EPSG:3011)
 - data/normalized/scb/TAB<id>.parquet  (UTF-8, typed, Stockholm-filtered)

Usage:
    uv run --with pandas --with pyarrow python scripts/normalize.py [--sbk] [--deso] [--scb]

Default is all three steps. Pass step flags to run a subset.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data"
OUT = RAW / "normalized"
OUT_SBK = OUT / "sbk"
OUT_DESO = OUT / "deso"
OUT_SCB = OUT / "scb"
OUT_MAPPINGS = OUT / "mappings"
MAPPINGS_SRC = RAW / "scb_deso/mappings"

SBK_SHP_DIR = RAW / "stockholm_sbk/Stadskarta_hela_Stockholm/Stockholm_Utskr-Stadskarta-Standard_shp"
DESO_DIR = RAW / "scb_deso/geographic"
SCB_TABLES_DIR = RAW / "scb_deso/tables"

STOCKHOLM_KOMMUN_CODE = "0180"
TARGET_EPSG = 3011


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"\nCommand failed: {' '.join(cmd)}\n{r.stderr}\n")
        raise SystemExit(r.returncode)


def normalize_sbk() -> dict:
    print(f"[sbk] converting 30 shapefiles → {OUT_SBK}/")
    OUT_SBK.mkdir(parents=True, exist_ok=True)
    results = {}
    shps = sorted(SBK_SHP_DIR.glob("*.shp"))
    for i, shp in enumerate(shps, 1):
        out = OUT_SBK / f"{shp.stem}.gpkg"
        if out.exists():
            out.unlink()
        run([
            "ogr2ogr",
            "-f", "GPKG",
            "-lco", "ENCODING=UTF-8",
            "-nln", shp.stem,
            str(out), str(shp),
        ])
        results[shp.stem] = {"path": str(out.relative_to(RAW)), "bytes": out.stat().st_size}
        print(f"  [{i:>2}/{len(shps)}] {shp.stem:<26} → {out.stat().st_size/1024:>8.0f} KB")
    return results


def normalize_deso() -> dict:
    """Filter to Stockholm kommun, reproject EPSG:3006 → EPSG:3011, and
    rename the geometry column `sp_geometry` → `geom` so it matches every
    other normalized GPKG. This lets the LLM use `geom` unconditionally in
    execute_sql joins without looking up the column per dataset."""
    print(f"[deso] filtering kommunkod='{STOCKHOLM_KOMMUN_CODE}', "
          f"reprojecting 3006→{TARGET_EPSG}, renaming sp_geometry→geom")
    OUT_DESO.mkdir(parents=True, exist_ok=True)
    results = {}
    for name in ("DeSO_2018.gpkg", "DeSO_2025.gpkg"):
        src = DESO_DIR / name
        dst = OUT_DESO / name
        if dst.exists():
            dst.unlink()
        layer_name = Path(name).stem
        # Use -sql to control output column order + rename; SQLite dialect
        # (default for GDAL vector SQL) lets us alias the geometry column.
        sql = (
            f"SELECT objectid, objektidentitet, objekttyp, desokod, regsokod, "
            f"lanskod, kommunkod, version, ansvarig_organisation, referensdatum, "
            f"sp_geometry AS geom FROM {layer_name} "
            f"WHERE kommunkod = '{STOCKHOLM_KOMMUN_CODE}'"
        )
        run([
            "ogr2ogr",
            "-f", "GPKG",
            "-t_srs", f"EPSG:{TARGET_EPSG}",
            "-sql", sql,
            "-dialect", "OGRSQL",
            "-nln", layer_name,
            "-lco", "GEOMETRY_NAME=geom",
            str(dst), str(src),
        ])
        results[layer_name] = {"path": str(dst.relative_to(RAW)), "bytes": dst.stat().st_size}
        print(f"  {name:<18} → {dst.stat().st_size/1024:>8.1f} KB")
    return results


def normalize_scb() -> dict:
    """Read each cp1252 CSV in 500k-row chunks, filter to Stockholm-relevant
    rows, write zstd-compressed parquet."""
    try:
        import pandas as pd  # noqa
    except ImportError:
        sys.stderr.write(
            "pandas not available — re-run with:\n"
            "  uv run --with pandas --with pyarrow python scripts/normalize.py\n"
        )
        raise SystemExit(1)
    import pandas as pd
    print(f"[scb] filtering 31 CSVs to Stockholm + '00 Riket' → {OUT_SCB}/")
    OUT_SCB.mkdir(parents=True, exist_ok=True)

    # Precompute canonical join-key lookups used at the end of each table's
    # normalize pass. Enables adding `desokod` / `desokod_2025` / `regsokod`
    # / `regso_name` / `kommunkod` / `kommun_name` uniformly across every
    # SCB parquet so LLM joins don't need per-table schema lookups.
    mappings_dir = OUT / "mappings"
    regso_map: dict[str, dict] = {}
    if (mappings_dir / "deso_regso_mapping.parquet").exists():
        dfm = pd.read_parquet(mappings_dir / "deso_regso_mapping.parquet")
        for row in dfm.itertuples(index=False):
            regso_map[row.desokod] = {
                "regsokod": row.regsokod,
                "regso_name": row.regso_name,
                "kommunkod": row.kommunkod,
                "kommun_name": row.kommunnamn,
            }
        print(f"[scb] loaded {len(regso_map):,} DeSO → RegSO/kommun entries")
    # 2018 → 2025 bridge. Some 2018 codes split into several 2025 codes;
    # we pick the first (deterministic by source order) and surface the
    # full list on layer-level notes via the audit output.
    bridge_2018_2025: dict[str, str] = {}
    if (mappings_dir / "deso_historical_changes.parquet").exists():
        dfh = pd.read_parquet(mappings_dir / "deso_historical_changes.parquet")
        for row in dfh.itertuples(index=False):
            bridge_2018_2025.setdefault(row.deso_2018, row.deso_2025)
        print(f"[scb] loaded {len(bridge_2018_2025):,} DeSO 2018→2025 bridge entries")

    results = {}
    tabs = sorted(p for p in SCB_TABLES_DIR.iterdir() if p.is_dir())
    for i, tab_dir in enumerate(tabs, 1):
        csvs = list(tab_dir.glob("*.csv"))
        if not csvs:
            continue
        src = csvs[0]
        dst = OUT_SCB / f"{tab_dir.name}.parquet"
        if dst.exists():
            dst.unlink()

        pieces = []
        total = 0
        for chunk in pd.read_csv(
            src,
            encoding="cp1252",
            chunksize=500_000,
            na_values=[".."],
            dtype=str,  # read everything as string first
        ):
            total += len(chunk)
            # SCB emits some categorical columns with trailing whitespace
            # (e.g. 'löneinkomst '). Strip every string column so downstream
            # `col = 'value'` queries work without TRIM. (pandas 3.x uses
            # StringDtype by default, not object, so check explicitly.)
            for c in chunk.columns:
                if pd.api.types.is_string_dtype(chunk[c]):
                    chunk[c] = chunk[c].str.strip()
            region = chunk["region"].astype(str)
            mask = (
                region.str.startswith("0180")
                | region.str.startswith("Stockholm (")
                | (region == "00 Riket")
            )
            pieces.append(chunk[mask])
        df = pd.concat(pieces, ignore_index=True)

        # Cast the last SCB column to numeric — it's the "value" column but
        # its name varies per table (SCB uses the first variable's label,
        # e.g. "Andel av befolkningen i inkomstklass"). '..' → NaN via na_values.
        raw_value_col = df.columns[-1]
        df[raw_value_col] = pd.to_numeric(df[raw_value_col], errors="coerce")

        # Rename to a stable `value` column across every SCB parquet. The
        # meaning of the row still lives in `tabellinnehåll`/similar columns.
        if raw_value_col != "value":
            df = df.rename(columns={raw_value_col: "value"})
        value_col = "value"

        # Add a region_kind / region_code / region_name discriminator derived
        # from the overloaded `region` column. SCB mixes DeSO codes (9 chars,
        # start with kommunkod), RegSO labels ("Stockholm (foo)"),
        # kommun codes (4 digits), and the national aggregate ("00 Riket").
        import re as _re
        region = df["region"].astype(str)
        # DeSO: <4-digit kommunkod><A-Z><4 digits>  (e.g. "0180A1010")
        deso_re = _re.compile(r"^\d{4}[A-Z]\d{4}$")
        is_deso = region.apply(lambda r: bool(deso_re.match(r)))
        is_country = region == "00 Riket"
        is_regso = region.str.startswith("Stockholm (")
        is_kommun = (~is_deso) & region.str.match(r"^\d{4}( .*)?$", na=False)

        def _kind(r: str) -> str:
            if deso_re.match(r): return "deso"
            if r == "00 Riket": return "country"
            if r.startswith("Stockholm ("): return "regso"
            if _re.match(r"^\d{4}( .*)?$", r): return "kommun"
            return "other"

        def _code(r: str) -> str:
            m = deso_re.match(r)
            if m: return r
            if r == "00 Riket": return "00"
            if r.startswith("Stockholm ("):
                # Stockholm (foo) — no numeric code; derive a stable key
                inner = r[len("Stockholm ("):].rstrip(")")
                return f"sthlm:{inner}"
            m = _re.match(r"^(\d{4})", r)
            if m: return m.group(1)
            return r

        def _name(r: str) -> str:
            if r.startswith("Stockholm ("):
                return r[len("Stockholm ("):].rstrip(")")
            if r == "00 Riket":
                return "Riket"
            return r

        df["region_kind"] = region.apply(_kind)
        df["region_code"] = region.apply(_code)
        df["region_name"] = region.apply(_name)

        # Canonical join-key columns — populated only when region_kind='deso',
        # NULL elsewhere. The raw region column stays intact for provenance.
        is_deso_mask = df["region_kind"] == "deso"
        df["desokod"] = df["region_code"].where(is_deso_mask, None)
        # 2018 → 2025 bridge. If the DeSO code is already a 2025 code (no
        # entry in the split table) or unchanged since 2018, desokod_2025
        # equals desokod.
        def _bridge(code):
            if code is None:
                return None
            return bridge_2018_2025.get(code, code)
        df["desokod_2025"] = df["desokod"].apply(_bridge)
        # RegSO / kommun lookup via desokod.
        def _regso(code, field):
            if code is None:
                return None
            entry = regso_map.get(code)
            if entry is None:
                return None
            return entry.get(field)
        df["regsokod"]    = df["desokod"].apply(lambda c: _regso(c, "regsokod"))
        df["regso_name"]  = df["desokod"].apply(lambda c: _regso(c, "regso_name"))
        df["kommunkod"]   = df["desokod"].apply(lambda c: _regso(c, "kommunkod"))
        df["kommun_name"] = df["desokod"].apply(lambda c: _regso(c, "kommun_name"))

        # Some SCB tables publish duplicate rows for the same dimension keys
        # (one with a suppressed/NaN value, one with the real value). Collapse
        # to one row per key combination preferring the non-null value.
        key_cols = [c for c in df.columns if c != value_col]
        before = len(df)
        df = df.sort_values(value_col, na_position="first").drop_duplicates(
            subset=key_cols, keep="last"
        ).reset_index(drop=True)
        if before != len(df):
            print(f"    deduped SCB rows: {before:,} → {len(df):,} "
                  f"(-{before - len(df):,}, keeping non-null values)")
        # Also cast 'år' (year) if present and fully parseable as int.
        for candidate in ("år", "ar"):
            if candidate in df.columns:
                try:
                    df[candidate] = pd.to_numeric(df[candidate])
                except (ValueError, TypeError):
                    pass

        df.to_parquet(dst, compression="zstd", index=False)
        kept = len(df)
        size = dst.stat().st_size
        results[tab_dir.name] = {
            "path": str(dst.relative_to(RAW)),
            "bytes": size,
            "rows_total": total,
            "rows_kept": kept,
            "columns": list(df.columns),
        }
        print(f"  [{i:>2}/{len(tabs)}] {tab_dir.name:<10} rows {total:>10,} → {kept:>8,}  "
              f"size {size/1024/1024:>7.2f} MB")
    return results


def normalize_mappings() -> dict:
    """Convert SCB's DeSO 2018↔2025 historical-changes + DeSO↔RegSO
    connection-table XLSX files to parquet."""
    try:
        import pandas as pd
    except ImportError:
        sys.stderr.write(
            "pandas not available — re-run with:\n"
            "  uv run --with pandas --with pyarrow --with openpyxl "
            "python scripts/normalize.py\n"
        )
        raise SystemExit(1)
    print(f"[mappings] converting SCB XLSX → {OUT_MAPPINGS}/")
    OUT_MAPPINGS.mkdir(parents=True, exist_ok=True)

    results: dict = {}
    # 1. historical changes: columns = Kommun, Kommunnamn, Tidigare DeSO, DeSO,
    #    Förändringstyp, Datum för förändring. First 2 rows are a title + header.
    src = MAPPINGS_SRC / "deso-historiska-forandringar-2025-09-19.xlsx"
    if src.exists():
        df = pd.read_excel(src, skiprows=2)
        # Normalize column names to snake_case English.
        df = df.rename(columns={
            "Kommun": "kommunkod",
            "Kommunnamn": "kommunnamn",
            "Tidigare DeSO": "deso_2018",
            "DeSO": "deso_2025",
            "Förändringstyp": "forandringstyp",
            "Datum för förändring": "datum",
        })
        # Pad kommunkod to 4 digits (SCB writes 180, we want '0180')
        df["kommunkod"] = df["kommunkod"].astype(str).str.zfill(4)
        for c in ("deso_2018", "deso_2025", "forandringstyp", "kommunnamn"):
            if c in df.columns:
                df[c] = df[c].astype(str).str.strip()
        df["datum"] = pd.to_datetime(df["datum"], errors="coerce").dt.date.astype(str)
        out = OUT_MAPPINGS / "deso_historical_changes.parquet"
        df.to_parquet(out, compression="zstd", index=False)
        results["deso_historical_changes"] = {
            "path": str(out.relative_to(RAW)), "bytes": out.stat().st_size,
            "rows": len(df),
        }
        print(f"  deso_historical_changes: {len(df):,} rows → {out.stat().st_size/1024:.1f} KB")

    # 2. DeSO↔RegSO connection: header is on row 4 (skiprows=3).
    src = MAPPINGS_SRC / "kopplingstabell-deso_regso-2025-03-21.xlsx"
    if src.exists():
        df = pd.read_excel(src, skiprows=3)
        df = df.rename(columns={
            "Kommun": "kommunkod",
            "Kommunnamn": "kommunnamn",
            "DeSO_2025": "desokod",
            "RegSO_2025": "regso_name",
            "RegSOkod": "regsokod",
        })
        df["kommunkod"] = df["kommunkod"].astype(str).str.zfill(4)
        for c in ("desokod", "regso_name", "regsokod", "kommunnamn"):
            if c in df.columns:
                df[c] = df[c].astype(str).str.strip()
        out = OUT_MAPPINGS / "deso_regso_mapping.parquet"
        df.to_parquet(out, compression="zstd", index=False)
        results["deso_regso_mapping"] = {
            "path": str(out.relative_to(RAW)), "bytes": out.stat().st_size,
            "rows": len(df),
        }
        print(f"  deso_regso_mapping: {len(df):,} rows → {out.stat().st_size/1024:.1f} KB")

    return results


def smoke_test() -> None:
    """Assert invariants on the normalized output."""
    print("[smoke] verifying normalized outputs…")
    try:
        from osgeo import ogr, osr
        ogr.UseExceptions()
    except ImportError:
        print("  [skip] GDAL python bindings unavailable")
        return

    failures: list[str] = []

    for gpkg in sorted(OUT_SBK.glob("*.gpkg")):
        ds = ogr.Open(str(gpkg))
        srs = ds.GetLayer(0).GetSpatialRef()
        if srs and srs.GetAuthorityCode(None) != str(TARGET_EPSG):
            failures.append(f"SBK {gpkg.name}: expected EPSG:{TARGET_EPSG}, got {srs.GetAuthorityCode(None)}")
    for gpkg in sorted(OUT_DESO.glob("*.gpkg")):
        ds = ogr.Open(str(gpkg))
        lyr = ds.GetLayer(0)
        srs = lyr.GetSpatialRef()
        if srs and srs.GetAuthorityCode(None) != str(TARGET_EPSG):
            failures.append(f"DeSO {gpkg.name}: expected EPSG:{TARGET_EPSG}, got {srs.GetAuthorityCode(None)}")
        lyr.SetAttributeFilter(f"kommunkod = '{STOCKHOLM_KOMMUN_CODE}'")
        n = lyr.GetFeatureCount()
        if n < 500 or n > 700:  # expect ~544 (2018) or ~569 (2025)
            failures.append(f"DeSO {gpkg.name}: unexpected Stockholm feature count {n}")

    # Axis-order canary: Stockholm City Hall
    src = osr.SpatialReference(); src.ImportFromEPSG(TARGET_EPSG)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference(); dst.ImportFromEPSG(3006)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    fwd = osr.CoordinateTransformation(src, dst)
    back = osr.CoordinateTransformation(dst, src)
    x1, y1, _ = fwd.TransformPoint(150893.0, 6579942.0)
    x2, y2, _ = back.TransformPoint(x1, y1)
    if abs(x2 - 150893.0) > 1 or abs(y2 - 6579942.0) > 1:
        failures.append(f"axis-order canary: roundtrip drifted ({x2:.2f}, {y2:.2f})")
    if not (650_000 < x1 < 700_000 and 6_560_000 < y1 < 6_600_000):
        failures.append(f"axis-order canary: 3011→3006 produced nonsense ({x1:.2f}, {y1:.2f})")

    if failures:
        print("  FAIL:")
        for f in failures:
            print(f"    - {f}")
        raise SystemExit(1)
    print("  all invariants hold")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sbk", action="store_true", help="only run SBK step")
    parser.add_argument("--deso", action="store_true", help="only run DeSO step")
    parser.add_argument("--scb", action="store_true", help="only run SCB step")
    parser.add_argument("--mappings", action="store_true", help="only run the SCB XLSX mapping step")
    parser.add_argument("--no-smoke", action="store_true", help="skip smoke test")
    parser.add_argument("--no-audit", action="store_true", help="skip catalog + cross-ref audits")
    args = parser.parse_args()

    run_all = not (args.sbk or args.deso or args.scb or args.mappings)
    OUT.mkdir(parents=True, exist_ok=True)
    manifest: dict = {}

    if run_all or args.sbk:
        manifest["sbk"] = normalize_sbk()
    if run_all or args.deso:
        manifest["deso"] = normalize_deso()
    if run_all or args.scb:
        manifest["scb"] = normalize_scb()
    if run_all or args.mappings:
        manifest["mappings"] = normalize_mappings()

    manifest_path = OUT / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"\nwrote manifest: {manifest_path.relative_to(ROOT)}")

    if not args.no_smoke:
        smoke_test()

    if not args.no_audit:
        # catalog_audit + cross_ref_audit both exit non-zero on ERROR. Propagate
        # so CI / scripts fail loudly on regressions.
        import subprocess
        for script in ("catalog_audit.py", "cross_ref_audit.py"):
            print()
            result = subprocess.run(
                ["uv", "run", "python", str(ROOT / "scripts" / script)]
            )
            if result.returncode != 0:
                raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
