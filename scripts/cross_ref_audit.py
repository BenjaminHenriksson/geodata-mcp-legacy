"""Cross-reference audit: every DeSO code in each SCB parquet must resolve
to a polygon in DeSO_2025 OR DeSO_2018 OR via the SCB historical-changes
mapping table (`deso_historical_changes`).

If a code is in none of these, it points at either:
  - A data-quality bug in the SCB extract
  - A DeSO code we failed to load (kommunkod filter too narrow?)
  - A stale DeSO that SCB removed without a mapping entry

Run: uv run python scripts/cross_ref_audit.py
Exit 0 if all region codes resolve; 1 otherwise.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog.json"
NORM = ROOT / "data/normalized"
DESO_2018 = NORM / "deso/DeSO_2018.gpkg"
DESO_2025 = NORM / "deso/DeSO_2025.gpkg"
HIST = NORM / "mappings/deso_historical_changes.parquet"


def _deso_codes_from_gpkg(con, path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {r[0] for r in con.execute(
        f"SELECT desokod FROM ST_Read('{path}')"
    ).fetchall()}


def _hist_mapped_codes(con, path: Path) -> set[str]:
    """Union of DeSO codes mentioned on either side of SCB's change log."""
    if not path.exists():
        return set()
    rows = con.execute(
        f"SELECT deso_2018, deso_2025 FROM read_parquet('{path}')"
    ).fetchall()
    out: set[str] = set()
    for a, b in rows:
        if a:
            out.add(a)
        if b:
            out.add(b)
    return out


def _is_deso_code(s: str) -> bool:
    # 4 digits + letter A/B/C + 4 digits
    if not s or len(s) != 9:
        return False
    return (s[:4].isdigit()
            and s[4] in "ABC"
            and s[5:].isdigit())


def audit() -> list[tuple[str, str, str]]:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    codes_2018 = _deso_codes_from_gpkg(con, DESO_2018)
    codes_2025 = _deso_codes_from_gpkg(con, DESO_2025)
    codes_hist = _hist_mapped_codes(con, HIST)

    known = codes_2018 | codes_2025 | codes_hist

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    issues: list[tuple[str, str, str]] = []

    for d in catalog["datasets"]:
        if d["source_type"] != "parquet":
            continue
        # Only check SCB stat tables — the OSM and other-source parquets
        # don't carry a `region` column.
        if not d.get("file_path", "").startswith("data/normalized/scb/"):
            continue
        file_path = ROOT / d["file_path"]
        if not file_path.exists():
            continue
        # skip the mapping tables themselves
        if "/mappings/" in d["file_path"]:
            continue

        # Pull distinct region values that look like DeSO codes
        rows = con.execute(
            f"SELECT DISTINCT region FROM read_parquet('{file_path}') "
            f"WHERE region LIKE '____C____' OR region LIKE '____A____' OR region LIKE '____B____'"
        ).fetchall()
        deso_codes_in_table = {r[0] for r in rows if _is_deso_code(r[0])}

        unresolved = deso_codes_in_table - known
        if unresolved:
            sample = sorted(unresolved)[:5]
            issues.append((d["id"], "ERROR",
                          f"{len(unresolved)} DeSO code(s) in this table resolve to no polygon and no historical-change entry; "
                          f"first {len(sample)}: {sample}"))

        only_2018 = deso_codes_in_table & codes_2018 - codes_2025
        only_2025 = deso_codes_in_table & codes_2025 - codes_2018
        via_hist = deso_codes_in_table & (codes_hist - codes_2018 - codes_2025)
        if unresolved or only_2018 or via_hist:
            issues.append((d["id"], "INFO",
                          f"coverage: total_codes={len(deso_codes_in_table)}, "
                          f"in_2025={len(deso_codes_in_table & codes_2025)}, "
                          f"only_in_2018={len(only_2018)}, "
                          f"only_via_hist={len(via_hist)}, "
                          f"unresolved={len(unresolved)}"))

    return issues


def main() -> int:
    issues = audit()
    if not issues:
        print("cross-ref audit: clean ✓ (every DeSO region code in SCB parquets resolves)")
        return 0
    by_ds: dict[str, list[tuple[str, str]]] = {}
    for ds_id, level, msg in issues:
        by_ds.setdefault(ds_id, []).append((level, msg))
    for ds_id, items in by_ds.items():
        print(f"\n{ds_id}")
        for level, msg in items:
            print(f"  [{level}] {msg}")
    errors = [i for i in issues if i[1] == "ERROR"]
    print(f"\n{len(errors)} error(s), {len(issues) - len(errors)} info message(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
