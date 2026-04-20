"""Export ops: export_layer, export_layers, export_and_cite (+ share-token
purger)."""
from __future__ import annotations

import secrets
import time
from datetime import datetime
from pathlib import Path
from pathlib import Path as _Path

from ..loader import LoadError as OpError, _quote_ident
from ..session import Operation, Session, SourceRef
from ._util import _require_layer


EXPORT_ROOT = _Path(__file__).resolve().parents[2] / "data" / "exports"
EXPORT_TTL_S = 24 * 60 * 60   # 24 h

VALID_EXPORT_FORMATS = {"geojson", "gpkg", "csv", "parquet"}


def _purge_expired_exports() -> int:
    if not EXPORT_ROOT.exists():
        return 0
    now = time.time()
    removed = 0
    for token_dir in EXPORT_ROOT.iterdir():
        if not token_dir.is_dir():
            continue
        try:
            if now - token_dir.stat().st_mtime > EXPORT_TTL_S:
                for f in token_dir.iterdir():
                    f.unlink()
                token_dir.rmdir()
                removed += 1
        except OSError:
            pass
    return removed


def _export_safe_select_list(
    conn, layer: str, attr_cols: list[str], geom_col: str | None = None,
    geom_transform: bool = False,
) -> str:
    """Build a SELECT list for a layer that's safe to feed into GDAL/GeoJSON/
    CSV/Parquet COPY statements. Wraps HUGEINT columns in `CAST(... AS BIGINT)`
    because GeoJSON's JSON-number serializer and GDAL's Integer64 field
    type only go up to ~19 digits of precision (64 bits). HUGEINT commonly
    arises from aggregate expressions: `SUM(CAST(x AS BIGINT))` auto-
    promotes to HUGEINT in DuckDB, which then fails a non-descriptive
    "For decimal field, only precision up to 19 is supported" at export.

    Args:
      conn: live DuckDB connection.
      layer: quoted or unquoted table name (we requote internally).
      attr_cols: attribute columns to select, IN ORDER.
      geom_col: if set, append a geometry expression (ST_Transform to 4326
                when geom_transform=True, else the column raw).
      geom_transform: reproject 3011 → 4326 for the geom column.

    Returns a SELECT list fragment (no leading "SELECT", no trailing FROM).
    """
    qlayer = _quote_ident(layer)
    type_rows = conn.execute(f"DESCRIBE {qlayer}").fetchall()
    col_types = {r[0]: r[1].upper() for r in type_rows}
    parts: list[str] = []
    for c in attr_cols:
        qc = _quote_ident(c)
        ctype = col_types.get(c, "")
        if ctype == "HUGEINT" or ctype.startswith("HUGEINT"):
            # Safe — if the value actually exceeds BIGINT range it'll
            # surface as an explicit overflow, not a cryptic precision error.
            parts.append(f"CAST({qc} AS BIGINT) AS {qc}")
        elif ctype == "UHUGEINT" or ctype.startswith("UHUGEINT"):
            parts.append(f"CAST({qc} AS UBIGINT) AS {qc}")
        else:
            parts.append(qc)
    if geom_col:
        qg = _quote_ident(geom_col)
        if geom_transform:
            parts.append(
                f"ST_Transform({qg}, 'EPSG:3011', 'EPSG:4326', true) AS geom"
            )
        else:
            parts.append(f"{qg} AS geom")
    return ", ".join(parts) if parts else "1"


def _write_single_layer(
    session: Session, layer: str, dest: Path, fmt: str,
) -> None:
    """Emit one layer's COPY into `dest`. Shared by export_layer and
    export_many for single-file-per-layer formats (geojson/csv/parquet)."""
    meta = _require_layer(session, layer)
    geom_col = meta.attributes.get("__geom_col__") or ""
    attr_cols = [c for c in meta.attributes
                 if not c.startswith("__") and c != geom_col]
    qlayer = _quote_ident(layer)
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in layer)

    if fmt == "geojson":
        if not geom_col:
            raise OpError(f"layer '{layer}' has no geometry; cannot export as GeoJSON")
        sel = _export_safe_select_list(
            session.conn, layer, attr_cols,
            geom_col=geom_col, geom_transform=True,
        )
        sql = (
            f"COPY (SELECT {sel} FROM {qlayer}) "
            f"TO '{dest}' (FORMAT GDAL, DRIVER 'GeoJSON', SRS 'EPSG:4326')"
        )
    elif fmt == "gpkg":
        if not geom_col:
            raise OpError(f"layer '{layer}' has no geometry; cannot export as GPKG")
        sel = _export_safe_select_list(
            session.conn, layer, attr_cols,
            geom_col=geom_col, geom_transform=False,
        )
        sql = (
            f"COPY (SELECT {sel} FROM {qlayer}) "
            f"TO '{dest}' (FORMAT GDAL, DRIVER 'GPKG', "
            f"LAYER_NAME '{safe_name}', SRS 'EPSG:3011')"
        )
    elif fmt == "csv":
        if geom_col:
            sel = _export_safe_select_list(session.conn, layer, attr_cols)
            sel = (f"{sel + ',' if attr_cols else ''} "
                   f"ST_AsText({_quote_ident(geom_col)}) AS geom_wkt")
        else:
            sel = _export_safe_select_list(session.conn, layer, attr_cols)
        sql = f"COPY (SELECT {sel} FROM {qlayer}) TO '{dest}' (HEADER, DELIMITER ',')"
    else:  # parquet
        sel = _export_safe_select_list(session.conn, layer, attr_cols,
                                        geom_col=geom_col, geom_transform=False)
        sql = f"COPY (SELECT {sel} FROM {qlayer}) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)"

    try:
        session.conn.execute(sql)
    except Exception as e:
        raise OpError(f"export failed for layer '{layer}': {type(e).__name__}: {e}")


def export_layer(
    session: Session, layer: str, fmt: str = "geojson",
) -> dict:
    """Write a layer to data/exports/<token>/<name>.<ext> and return its URL + size.

    Formats:
      geojson — EPSG:4326 GeoJSON FeatureCollection
      gpkg    — OGC GeoPackage in native EPSG:3011
      csv     — attributes only + geom serialized as WKT
      parquet — columnar, geometry as WKB
    """
    fmt = fmt.lower()
    if fmt not in VALID_EXPORT_FORMATS:
        raise OpError(
            f"unsupported format '{fmt}'. Supported: {sorted(VALID_EXPORT_FORMATS)}"
        )
    meta = _require_layer(session, layer)

    _purge_expired_exports()
    token = secrets.token_urlsafe(16)
    dest_dir = EXPORT_ROOT / token
    dest_dir.mkdir(parents=True, exist_ok=True)

    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in layer)
    file_name = f"{safe_name}.{fmt}"
    dest = dest_dir / file_name

    _write_single_layer(session, layer, dest, fmt)

    size_bytes = dest.stat().st_size
    expires_at = time.time() + EXPORT_TTL_S
    url = f"/exports/{token}/{file_name}"

    session.log(Operation(
        tool="export",
        args={"layer": layer, "format": fmt},
        result_layer=None,
        summary=f"{fmt} {size_bytes} bytes",
        at=datetime.utcnow(),
    ))
    return {
        "layer": layer,
        "format": fmt,
        "url": url,
        "file_name": file_name,
        "size_bytes": size_bytes,
        "expires_at": datetime.utcfromtimestamp(expires_at).isoformat() + "Z",
        "provenance": [
            {"dataset_id": s.dataset_id, "source_name": s.source_name,
             "publisher": s.publisher, "license": s.license, "url": s.url,
             "retrieved": s.retrieved, "llm_sourced": s.llm_sourced,
             "llm_source_description": s.llm_source_description}
            for s in meta.provenance
        ],
    }


def export_layers(
    session: Session, layers: list[str], fmt: str = "gpkg",
    merge_geojson: bool = False,
) -> dict:
    """Export multiple layers under a single 24-h download token.

    Shapes:
      - fmt='gpkg': one `.gpkg` file containing every layer (GeoPackage
        supports multi-layer natively). One URL returned.
      - fmt='geojson', merge_geojson=True: one `.geojson` file with a
        single FeatureCollection; every feature has a `_layer` property.
      - fmt='geojson'|'csv'|'parquet' (merge_geojson=False): one file per
        layer, all under the same token directory. List of URLs returned.

    Provenance is the deduped union of every source layer's SourceRefs.
    """
    fmt = fmt.lower()
    if fmt not in VALID_EXPORT_FORMATS:
        raise OpError(
            f"unsupported format '{fmt}'. Supported: {sorted(VALID_EXPORT_FORMATS)}"
        )
    if not layers:
        raise OpError("export_layers needs at least one layer")
    # Validate all layers up front, fail fast.
    metas = [_require_layer(session, l) for l in layers]

    _purge_expired_exports()
    token = secrets.token_urlsafe(16)
    dest_dir = EXPORT_ROOT / token
    dest_dir.mkdir(parents=True, exist_ok=True)

    def _safe(name: str) -> str:
        return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)

    files: list[dict] = []

    if fmt == "gpkg":
        # Multi-layer GeoPackage. DuckDB's COPY-with-APPEND silently
        # overwrites the file on GPKG, so the working recipe is:
        #   1. emit each layer to its own <tmp>/<layer>.gpkg via DuckDB
        #   2. merge with `ogr2ogr -update -append` into dest.
        # ogr2ogr ships with the gdal-bin package; GDAL is already a
        # runtime dep via the DuckDB spatial extension.
        import subprocess, tempfile
        from pathlib import Path as _P
        file_name = "export.gpkg"
        dest = dest_dir / file_name
        with tempfile.TemporaryDirectory(prefix="gpkg_merge_") as tmp:
            tmp_paths: list[tuple[str, _P]] = []
            for layer in layers:
                m = session.layers[layer]
                geom_col = m.attributes.get("__geom_col__") or ""
                if not geom_col:
                    raise OpError(
                        f"layer '{layer}' has no geometry; can't include in a GPKG export"
                    )
                attr_cols = [c for c in m.attributes
                             if not c.startswith("__") and c != geom_col]
                sel = _export_safe_select_list(
                    session.conn, layer, attr_cols,
                    geom_col=geom_col, geom_transform=False,
                )
                gdal_layer = _safe(layer)
                tmp_path = _P(tmp) / f"{gdal_layer}.gpkg"
                sql = (
                    f"COPY (SELECT {sel} FROM {_quote_ident(layer)}) "
                    f"TO '{tmp_path}' (FORMAT GDAL, DRIVER 'GPKG', "
                    f"LAYER_NAME '{gdal_layer}', SRS 'EPSG:3011')"
                )
                try:
                    session.conn.execute(sql)
                except Exception as e:
                    raise OpError(
                        f"gpkg export failed on layer '{layer}': {type(e).__name__}: {e}"
                    )
                tmp_paths.append((gdal_layer, tmp_path))
            # First layer: copy as the base file. Subsequent layers:
            # ogr2ogr -update -append.
            if tmp_paths:
                first_name, first_path = tmp_paths[0]
                import shutil as _sh
                _sh.copyfile(first_path, dest)
                for gdal_layer, tmp_path in tmp_paths[1:]:
                    r = subprocess.run(
                        ["ogr2ogr", "-f", "GPKG", "-update", "-append",
                         "-nln", gdal_layer, str(dest), str(tmp_path)],
                        capture_output=True, text=True,
                    )
                    if r.returncode != 0:
                        raise OpError(
                            f"gpkg merge failed on layer '{gdal_layer}': "
                            f"{r.stderr.strip()[:300]}"
                        )
        files.append({
            "layers": list(layers), "format": "gpkg",
            "file_name": file_name, "url": f"/exports/{token}/{file_name}",
            "size_bytes": dest.stat().st_size,
        })

    elif fmt == "geojson" and merge_geojson:
        # Single merged FeatureCollection with a _layer property per feature.
        # Build per-layer feature lists then assemble on disk to keep memory bounded.
        file_name = "export.geojson"
        dest = dest_dir / file_name
        with open(dest, "w", encoding="utf-8") as f:
            f.write('{"type":"FeatureCollection","features":[')
            first = True
            for layer in layers:
                m = session.layers[layer]
                geom_col = m.attributes.get("__geom_col__") or ""
                if not geom_col:
                    # Skip non-geometric layers in merged output with a warning
                    # appended to the response rather than failing.
                    continue
                attr_cols = [c for c in m.attributes
                             if not c.startswith("__") and c != geom_col]
                # Probe each column's type so we only wrap HUGEINT in CAST.
                type_rows = session.conn.execute(
                    f"DESCRIBE {_quote_ident(layer)}"
                ).fetchall()
                col_types = {r[0]: r[1].upper() for r in type_rows}

                def _prop_expr(c: str) -> str:
                    # Escape single quotes in the column-name literal the
                    # same way `escaped_layer` does below. Matters for
                    # correctness (columns like "King's Road" would break
                    # the SQL string) and blocks an injection surface — an
                    # LLM caller could supply a dict key like
                    # `x', (SELECT ...)::JSON, 'y` via create_layer and
                    # pass it through to this per-feature JSON emitter.
                    key = c.replace("'", "''")
                    qc = _quote_ident(c)
                    t = col_types.get(c, "")
                    if t == "HUGEINT" or t.startswith("HUGEINT"):
                        return f"'{key}', CAST({qc} AS BIGINT)"
                    if t == "UHUGEINT" or t.startswith("UHUGEINT"):
                        return f"'{key}', CAST({qc} AS UBIGINT)"
                    return f"'{key}', {qc}"

                escaped_layer = layer.replace("'", "''")
                if attr_cols:
                    props_parts = ", ".join(_prop_expr(c) for c in attr_cols)
                    props_expr = (
                        f"json_object('_layer', '{escaped_layer}', {props_parts})"
                    )
                else:
                    props_expr = f"json_object('_layer', '{escaped_layer}')"
                per_feat_sql = (
                    f"SELECT json_object("
                    f"'type', 'Feature', "
                    f"'properties', {props_expr}, "
                    f"'geometry', ST_AsGeoJSON(ST_Transform("
                    f"{_quote_ident(geom_col)}, 'EPSG:3011', 'EPSG:4326', true))::JSON"
                    f")::VARCHAR "
                    f"FROM {_quote_ident(layer)}"
                )
                cur = session.conn.execute(per_feat_sql)
                while True:
                    rows = cur.fetchmany(2000)
                    if not rows:
                        break
                    for (feat,) in rows:
                        if feat is None:
                            continue
                        if not first:
                            f.write(",")
                        first = False
                        f.write(feat)
            f.write("]}")
        files.append({
            "layers": list(layers), "format": "geojson", "merged": True,
            "file_name": file_name, "url": f"/exports/{token}/{file_name}",
            "size_bytes": dest.stat().st_size,
        })

    else:
        # One file per layer, all under the same token dir.
        for layer in layers:
            safe_name = _safe(layer)
            file_name = f"{safe_name}.{fmt}"
            dest = dest_dir / file_name
            _write_single_layer(session, layer, dest, fmt)
            files.append({
                "layer": layer, "format": fmt,
                "file_name": file_name, "url": f"/exports/{token}/{file_name}",
                "size_bytes": dest.stat().st_size,
            })

    # Merged provenance (dedup via key).
    seen: dict[tuple, SourceRef] = {}
    for m in metas:
        for s in m.provenance:
            key = (s.dataset_id, s.source_name, s.file_path, s.llm_sourced)
            seen.setdefault(key, s)
    prov = [
        {"dataset_id": s.dataset_id, "source_name": s.source_name,
         "publisher": s.publisher, "license": s.license, "url": s.url,
         "retrieved": s.retrieved, "llm_sourced": s.llm_sourced,
         "llm_source_description": s.llm_source_description}
        for s in seen.values()
    ]

    expires_at = time.time() + EXPORT_TTL_S
    session.log(Operation(
        tool="export_many",
        args={"layers": list(layers), "format": fmt,
              "merge_geojson": merge_geojson},
        result_layer=None,
        summary=f"{fmt} × {len(files)} file(s)",
        at=datetime.utcnow(),
    ))
    return {
        "format": fmt,
        "merge_geojson": merge_geojson,
        "files": files,
        "token": token,
        "expires_at": datetime.utcfromtimestamp(expires_at).isoformat() + "Z",
        "provenance": prov,
    }


def export_and_cite(
    session: Session, layer: str, fmt: str = "gpkg",
) -> dict:
    """Run `export_layer` and `sources(layer)` in one call. Saves the
    last round-trip of every analysis workflow."""
    # Lazy import — sources() lives in inspect.py which is a sibling module.
    from .inspect import sources

    exported = export_layer(session, layer, fmt=fmt)
    citations = sources(session, layer=layer)
    return {**exported, "citations_markdown": citations}
