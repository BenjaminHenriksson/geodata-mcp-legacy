"""Estimate storage savings from filtering the DeSO bundle to Stockholm-only
(region/kommunkod prefix = 0180). Does NOT modify any source file.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TABLES = DATA / "scb_deso/tables"
GEO = DATA / "scb_deso/geographic"

STOCKHOLM_PREFIX = "0180"


def estimate_csv(csv_path: Path) -> dict:
    total_bytes = csv_path.stat().st_size
    # First column (region) is always double-quoted in the DeSO bulk CSVs.
    total_rows = 0
    header_bytes = 0
    match_bytes = 0
    match_rows = 0
    with csv_path.open("rb") as f:
        header = f.readline()
        header_bytes = len(header)
        needle_prefix = f'"{STOCKHOLM_PREFIX}'.encode("cp1252")
        for line in f:
            total_rows += 1
            if line.startswith(needle_prefix):
                match_rows += 1
                match_bytes += len(line)
    kept = header_bytes + match_bytes
    return {
        "file": csv_path.name,
        "total_bytes": total_bytes,
        "total_rows": total_rows,
        "match_rows": match_rows,
        "kept_bytes_estimate": kept,
        "removed_bytes_estimate": total_bytes - kept,
    }


def estimate_geopackage(gpkg: Path) -> dict:
    total_bytes = gpkg.stat().st_size
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / f"stockholm_{gpkg.stem}.gpkg"
        subprocess.run(
            [
                "ogr2ogr",
                "-f", "GPKG",
                str(out), str(gpkg),
                "-where", f"kommunkod = '{STOCKHOLM_PREFIX}'",
            ],
            check=True,
            capture_output=True,
        )
        filtered_bytes = out.stat().st_size
        # VACUUM to remove sqlite slack and get a tight estimate
        vacuumed = Path(td) / f"stockholm_{gpkg.stem}_vac.gpkg"
        subprocess.run(
            ["ogr2ogr", "-f", "GPKG", str(vacuumed), str(out)],
            check=True, capture_output=True,
        )
        filtered_vac_bytes = vacuumed.stat().st_size
    return {
        "file": gpkg.name,
        "total_bytes": total_bytes,
        "filtered_bytes": filtered_bytes,
        "filtered_vacuumed_bytes": filtered_vac_bytes,
        "removed_bytes_estimate": total_bytes - filtered_vac_bytes,
    }


def fmt(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> None:
    print("=== Geographic (geopackages) ===")
    geo_removed = 0
    geo_total = 0
    for gpkg in sorted(GEO.glob("*.gpkg")):
        r = estimate_geopackage(gpkg)
        geo_total += r["total_bytes"]
        geo_removed += r["removed_bytes_estimate"]
        pct_removed = 100.0 * r["removed_bytes_estimate"] / r["total_bytes"]
        print(
            f"  {r['file']:<18}  current={fmt(r['total_bytes']):>9}  "
            f"stockholm-only={fmt(r['filtered_vacuumed_bytes']):>9}  "
            f"saves={fmt(r['removed_bytes_estimate']):>9}  ({pct_removed:.1f}%)"
        )

    print()
    print("=== Statistical tables (31 CSVs) — region prefix '0180' ===")
    print(f"  {'table':<10} {'total_rows':>12} {'match_rows':>12}  {'total':>10}  {'kept':>10}  {'saves':>10}  pct")
    tab_removed = 0
    tab_total = 0
    tab_total_rows = 0
    tab_match_rows = 0
    for tab_dir in sorted(TABLES.iterdir()):
        if not tab_dir.is_dir():
            continue
        csv_files = list(tab_dir.glob("*.csv"))
        if not csv_files:
            continue
        r = estimate_csv(csv_files[0])
        tab_total += r["total_bytes"]
        tab_removed += r["removed_bytes_estimate"]
        tab_total_rows += r["total_rows"]
        tab_match_rows += r["match_rows"]
        pct_removed = 100.0 * r["removed_bytes_estimate"] / r["total_bytes"]
        print(
            f"  {tab_dir.name:<10} {r['total_rows']:>12} {r['match_rows']:>12}  "
            f"{fmt(r['total_bytes']):>10}  {fmt(r['kept_bytes_estimate']):>10}  "
            f"{fmt(r['removed_bytes_estimate']):>10}  {pct_removed:5.1f}%"
        )

    print()
    print("=== Totals ===")
    grand_total = geo_total + tab_total
    grand_removed = geo_removed + tab_removed
    print(f"  geopackages total:          {fmt(geo_total):>10}   saves {fmt(geo_removed)} ({100*geo_removed/geo_total:.1f}%)")
    print(f"  CSV tables total:           {fmt(tab_total):>10}   saves {fmt(tab_removed)} ({100*tab_removed/tab_total:.1f}%)")
    print(f"  all DeSO on disk:           {fmt(grand_total):>10}")
    print(f"  estimated savings:          {fmt(grand_removed):>10}  ({100*grand_removed/grand_total:.2f}%)")
    print(f"  CSV row reduction:          {tab_total_rows:,} → {tab_match_rows:,}  "
          f"({100*tab_match_rows/tab_total_rows:.2f}% kept)")


if __name__ == "__main__":
    main()
