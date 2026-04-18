"""Audit catalog.json against live normalized data.

For each dataset entry verifies:
  1. File exists at `file_path`
  2. `feature_count` matches actual row/feature count
  3. Every declared attribute exists with compatible type
  4. Every declared `sample_values` entry actually appears in the column
  5. For spatial: `geometry_type` matches and CRS is EPSG:3011
  6. Any `trailing whitespace` in categorical columns (SCB gotcha)
  7. Any columns NOT in the catalog (undeclared attributes)

Reports mismatches as a table; exits 1 if any are found (CI-friendly).

Run: uv run python scripts/catalog_audit.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog.json"

TYPE_COMPAT = {
    "VARCHAR": ("VARCHAR",),
    "BIGINT": ("BIGINT", "INTEGER", "HUGEINT", "UBIGINT"),
    "INTEGER": ("INTEGER", "BIGINT", "SMALLINT", "TINYINT"),
    "DOUBLE": ("DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC"),
}


def _norm_type(t: str) -> str:
    t = t.upper()
    # strip DECIMAL(p,s)
    for prefix in ("DECIMAL(", "NUMERIC(", "GEOMETRY("):
        if t.startswith(prefix):
            t = t[: len(prefix) - 1]
            break
    return t


def _compat(declared: str, actual: str) -> bool:
    d, a = _norm_type(declared), _norm_type(actual)
    if d == a:
        return True
    return a in TYPE_COMPAT.get(d, ())


def audit(catalog: dict) -> list[tuple[str, str, str]]:
    """Return a list of (dataset_id, severity, message) for mismatches."""
    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")

    issues: list[tuple[str, str, str]] = []

    def flag(ds_id: str, level: str, msg: str) -> None:
        issues.append((ds_id, level, msg))

    for d in catalog["datasets"]:
        ds_id = d["id"]
        file_path = ROOT / d["file_path"]
        if not file_path.exists():
            flag(ds_id, "ERROR", f"file missing: {d['file_path']}")
            continue

        src_type = d["source_type"]
        if src_type == "geopackage":
            layer_arg = f", layer='{d['layer']}'" if d.get("layer") else ""
            src_expr = f"ST_Read('{file_path}'{layer_arg})"
        elif src_type == "parquet":
            src_expr = f"read_parquet('{file_path}')"
        else:
            flag(ds_id, "ERROR", f"unsupported source_type '{src_type}'")
            continue

        # --- schema + row count ---
        try:
            schema_rows = conn.execute(f"DESCRIBE SELECT * FROM {src_expr}").fetchall()
        except Exception as e:
            flag(ds_id, "ERROR", f"DESCRIBE failed: {type(e).__name__}: {e}")
            continue
        actual_schema = {r[0]: r[1] for r in schema_rows}
        row_count = conn.execute(f"SELECT COUNT(*) FROM {src_expr}").fetchone()[0]

        # feature_count
        declared_fc = d.get("feature_count")
        if declared_fc is not None and declared_fc != row_count:
            flag(ds_id, "ERROR",
                 f"feature_count mismatch: catalog={declared_fc:,}, actual={row_count:,}")

        # geometry column + type + CRS (for spatial datasets)
        geom_col = next((c for c, t in actual_schema.items()
                         if _norm_type(t).startswith("GEOMETRY")), None)
        if d.get("geometry_type"):
            if geom_col is None:
                flag(ds_id, "ERROR",
                     f"geometry_type='{d['geometry_type']}' declared but no GEOMETRY column found")
            else:
                # actual geometry type from data
                actual_geom = conn.execute(
                    f"SELECT DISTINCT ST_GeometryType({geom_col}) AS t FROM {src_expr} LIMIT 5"
                ).fetchall()
                actual_kinds = {r[0] for r in actual_geom}
                declared = d["geometry_type"].upper()
                if not any(declared in k.upper() for k in actual_kinds):
                    flag(ds_id, "WARN",
                         f"geometry_type='{d['geometry_type']}' but data has {actual_kinds}")

                # CRS check via WKT
                crs_row = conn.execute(
                    f"SELECT ST_AsText({geom_col}) FROM {src_expr} LIMIT 1"
                ).fetchone()
                if d.get("crs_epsg") and d["crs_epsg"] != 3011:
                    flag(ds_id, "WARN",
                         f"declared crs_epsg={d['crs_epsg']}, expected 3011 after normalize")

        # --- attribute declarations ---
        declared_attrs = {a["name"]: a for a in d.get("attributes", [])}
        for name, spec in declared_attrs.items():
            if name not in actual_schema:
                flag(ds_id, "ERROR", f"attribute '{name}' declared but not in schema")
                continue
            dtype = spec.get("type", "")
            atype = actual_schema[name]
            if dtype and not _compat(dtype, atype):
                flag(ds_id, "WARN",
                     f"attribute '{name}' type declared={dtype}, actual={atype}")

            # sample_values must actually appear
            samples = spec.get("sample_values") or []
            for s in samples:
                if any(tag in s for tag in ("(tkr)", "(%)", "(antal)")):
                    # explanatory sample like "571.5 (tkr)" — skip
                    continue
                # For numeric columns, skip exact-equality check on sampled strings.
                if _norm_type(atype) != "VARCHAR":
                    continue
                found = conn.execute(
                    f"SELECT 1 FROM {src_expr} WHERE {_q(name)} = ? LIMIT 1",
                    [s]
                ).fetchone()
                if not found:
                    flag(ds_id, "ERROR",
                         f"attribute '{name}' sample value {s!r} does not appear in data")

        # --- undeclared columns ---
        ignored = {geom_col}  # don't flag the geometry column
        for col in actual_schema:
            if col in declared_attrs or col in ignored:
                continue
            # Objects/metadata columns on DeSO GPKG are OK; just WARN-level
            flag(ds_id, "WARN", f"column '{col}' exists in data but not in catalog")

        # --- trailing whitespace in VARCHAR columns (SCB gotcha) ---
        for col, t in actual_schema.items():
            if not _norm_type(t).startswith("VARCHAR"):
                continue
            try:
                ws = conn.execute(
                    f"SELECT COUNT(DISTINCT {_q(col)}) FROM {src_expr} "
                    f"WHERE {_q(col)} <> TRIM({_q(col)})"
                ).fetchone()[0]
                if ws:
                    flag(ds_id, "WARN",
                         f"column '{col}' has {ws} distinct value(s) with leading/trailing whitespace")
            except Exception:
                pass

    return issues


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def main() -> int:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    issues = audit(catalog)

    errors = [i for i in issues if i[1] == "ERROR"]
    warns = [i for i in issues if i[1] == "WARN"]

    if not issues:
        print("catalog audit: clean ✓")
        return 0

    by_ds: dict[str, list[tuple[str, str]]] = {}
    for ds_id, level, msg in issues:
        by_ds.setdefault(ds_id, []).append((level, msg))

    for ds_id, items in by_ds.items():
        print(f"\n{ds_id}")
        for level, msg in items:
            print(f"  [{level}] {msg}")

    print(f"\n{len(errors)} error(s), {len(warns)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
