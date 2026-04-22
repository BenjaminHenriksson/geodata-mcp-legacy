"""Stress-test the 11-tool MCP surface.

Hits every (tool, op) sub-branch at least once plus a handful of
error paths, then runs a realistic end-to-end workflow. Uses the
shared "default" session, which is isolated from real user sessions
(those use hash UUIDs). Run it in-process against the imported
FastMCP instance rather than over the wire — no uvicorn required.

    uv run python scripts/stress_test_tools.py

Exits 0 on clean pass, 1 on any failure. A table of results is
printed to stdout at the end; failures are listed first.
"""
from __future__ import annotations

import asyncio
import json
import sys
import traceback
from typing import Any


# Collect results so we can print a summary at the end.
_results: list[dict] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    _results.append({"name": name, "ok": ok, "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ""))


def _unpack(res) -> Any:
    """Resolve either a structured_content dict or a JSON/text payload.

    FastMCP wraps str returns in {"result": "..."} structured content.
    We unwrap that here so the tests see the original string.
    """
    sc = getattr(res, "structured_content", None)
    if sc is not None:
        # Unwrap the single-key {"result": <str>} envelope FastMCP emits for
        # tools whose return type is str (sources, inspect.rows markdown, etc.).
        if isinstance(sc, dict) and set(sc.keys()) == {"result"} and isinstance(sc["result"], str):
            return sc["result"]
        return sc
    content = getattr(res, "content", None) or []
    if content and hasattr(content[0], "text"):
        text = content[0].text
        try:
            return json.loads(text)
        except Exception:
            return text
    return None


def _is_error(payload: Any) -> bool:
    return isinstance(payload, dict) and "error" in payload


async def _call(mcp, tool: str, args: dict) -> Any:
    r = await mcp.call_tool(tool, args)
    return _unpack(r)


async def run_coverage(mcp) -> None:
    print("\n=== Coverage matrix: every (tool, op) ===")

    # ---- catalog ----
    try:
        d = await _call(mcp, "catalog", {"query": "deso", "limit": 3})
        ok = isinstance(d, dict) and d.get("total_in_catalog", 0) > 0
        _record("catalog.search", ok, f"hits={len(d.get('results', []))}")
    except Exception as e:
        _record("catalog.search", False, repr(e))

    try:
        d = await _call(mcp, "catalog", {"id": "deso_2025"})
        _record("catalog.describe", d.get("id") == "deso_2025",
                f"n_attr={len(d.get('attributes', []))}")
    except Exception as e:
        _record("catalog.describe", False, repr(e))

    try:
        d = await _call(mcp, "catalog", {"id": "nosuch_dataset"})
        _record("catalog.describe(unknown)",
                _is_error(d) and d["error"] == "unknown_dataset",
                d.get("error"))
    except Exception as e:
        _record("catalog.describe(unknown)", False, repr(e))

    # ---- geocode ----
    try:
        d = await _call(mcp, "geocode", {"op": "forward", "name": "Gamla Stan", "limit": 1})
        _record("geocode.forward", bool(d.get("matches")),
                f"match={d['matches'][0]['name'] if d.get('matches') else None}")
    except Exception as e:
        _record("geocode.forward", False, repr(e))

    try:
        d = await _call(mcp, "geocode", {"op": "reverse", "x_3011": 153844.0, "y_3011": 6578679.0})
        _record("geocode.reverse", isinstance(d, dict) and "containing" in d,
                f"n_containing={len(d.get('containing', []))}")
    except Exception as e:
        _record("geocode.reverse", False, repr(e))

    try:
        d = await _call(mcp, "geocode", {"op": "bbox", "name": "Södermalm", "buffer_m": 100})
        _record("geocode.bbox", isinstance(d, dict) and "bbox_3011" in d,
                f"bbox={d.get('bbox_3011')}")
    except Exception as e:
        _record("geocode.bbox", False, repr(e))

    try:
        d = await _call(mcp, "geocode", {"op": "forward"})
        _record("geocode.forward(missing_arg)",
                _is_error(d) and d["error"] == "missing_arg", d.get("error"))
    except Exception as e:
        _record("geocode.forward(missing_arg)", False, repr(e))

    # ---- load ----
    try:
        d = await _call(mcp, "load", {"op": "catalog", "dataset_ids": ["deso_2025"]})
        _record("load.catalog(single)", d.get("n_loaded") == 1, f"n_loaded={d.get('n_loaded')}")
    except Exception as e:
        _record("load.catalog(single)", False, repr(e))

    try:
        d = await _call(mcp, "load", {"op": "catalog", "dataset_ids": ["deso_2018", "deso_historical_changes"]})
        _record("load.catalog(multi)", d.get("n_loaded") == 2, f"n_loaded={d.get('n_loaded')}")
    except Exception as e:
        _record("load.catalog(multi)", False, repr(e))

    try:
        d = await _call(mcp, "load", {
            "op": "inline",
            "data": [{"name": "Test Cafe", "geom_wkt": "POINT(18.0658 59.3177)"}],
            "source": "stress_test 2026-04-22",
            "geometry_column": "geom_wkt",
            "layer_name": "test_cafe",
        })
        _record("load.inline", d.get("n_loaded") == 1,
                f"layer={d['loaded'][0]['name'] if d.get('loaded') else None}")
    except Exception as e:
        _record("load.inline", False, repr(e))

    try:
        d = await _call(mcp, "load", {"op": "inline", "data": [{"x": 1}]})
        # Should fail because `source` is mandatory
        _record("load.inline(no_source)", _is_error(d), d.get("error"))
    except Exception as e:
        _record("load.inline(no_source)", False, repr(e))

    # ---- execute_sql ----
    try:
        d = await _call(mcp, "execute_sql", {"sql": "SELECT COUNT(*) AS n FROM deso_2025"})
        # Table mode: {mode: "table", rows: <int_count>, table_md: "..."}
        ok = (isinstance(d, dict) and d.get("mode") == "table"
              and "| 569 |" in (d.get("table_md") or ""))
        _record("execute_sql(count)", ok, f"rows_count={d.get('rows')}")
    except Exception as e:
        _record("execute_sql(count)", False, repr(e))

    try:
        d = await _call(mcp, "execute_sql", {
            "sql": "SELECT desokod, geom FROM deso_2025 LIMIT 5",
            "result_name": "deso_top5",
        })
        _record("execute_sql(materialize layer)", d.get("layer_name") == "deso_top5",
                f"feat={d.get('feature_count')} name={d.get('layer_name')}")
    except Exception as e:
        _record("execute_sql(materialize layer)", False, repr(e))

    try:
        d = await _call(mcp, "execute_sql", {"sql": "DROP TABLE deso_2025"})
        _record("execute_sql(DDL rejected)",
                _is_error(d) and d["error"] == "sql_rejected", d.get("error"))
    except Exception as e:
        _record("execute_sql(DDL rejected)", False, repr(e))

    # ---- derive ----
    derive_tests = [
        ("filter",    {"op": "filter", "layer": "deso_2025",
                       "where": "desokod LIKE '0180C%'", "result_name": "d_filt"}),
        # `by` is a bare expression; direction is controlled by `ascending`.
        ("top_n",     {"op": "top_n", "layer": "deso_2025", "by": "ST_Area(geom)",
                       "ascending": False, "n": 5, "result_name": "d_top"}),
        ("buffer",    {"op": "buffer", "layer": "d_top", "distance_m": 50,
                       "result_name": "d_buf"}),
        ("centroid",  {"op": "centroid", "layer": "d_top", "result_name": "d_cent"}),
        ("convex_hull(agg)", {"op": "convex_hull", "layer": "d_top", "aggregate": True,
                              "result_name": "d_hull"}),
        ("dissolve",  {"op": "dissolve", "layer": "d_top", "result_name": "d_diss"}),
        ("select_by_location",
                      {"op": "select_by_location", "layer": "d_top", "by_layer": "d_hull",
                       "predicate": "intersects", "result_name": "d_sel"}),
        ("clip",      {"op": "clip", "layer": "d_top", "by_layer": "d_hull",
                       "result_name": "d_clip"}),
        ("intersect", {"op": "intersect", "a_layer": "d_top", "b_layer": "d_hull",
                       "result_name": "d_int"}),
    ]
    for subname, args in derive_tests:
        try:
            d = await _call(mcp, "derive", args)
            ok = isinstance(d, dict) and "feature_count" in d
            _record(f"derive.{subname}", ok,
                    f"feat={d.get('feature_count')}")
        except Exception as e:
            _record(f"derive.{subname}", False, repr(e))

    try:
        d = await _call(mcp, "derive", {"op": "buffer", "layer": "d_top"})
        _record("derive.buffer(missing distance_m)",
                _is_error(d), d.get("error"))
    except Exception as e:
        _record("derive.buffer(missing distance_m)", False, repr(e))

    # Regression: top_n must accept trailing DESC/ASC in `by` (bug fixed
    # 2026-04-22 — server used to wrap the by-expression in parens and
    # append its own direction, producing `ORDER BY (col DESC) DESC`).
    try:
        d = await _call(mcp, "derive", {
            "op": "top_n", "layer": "deso_2025",
            "by": "ST_Area(geom) DESC NULLS LAST", "n": 3,
            "result_name": "d_embedded_dir",
        })
        _record("derive.top_n(embedded DESC NULLS LAST)",
                isinstance(d, dict) and d.get("feature_count") == 3,
                d.get("error") or f"feat={d.get('feature_count')}")
    except Exception as e:
        _record("derive.top_n(embedded DESC NULLS LAST)", False, repr(e))

    # Feature: select_by_location with bare point+radius (no by_layer).
    try:
        d = await _call(mcp, "derive", {
            "op": "select_by_location", "layer": "deso_2025",
            "center_3011": [153844.0, 6578679.0], "distance_m": 500.0,
            "result_name": "d_near_point",
        })
        _record("derive.select_by_location(point+radius)",
                isinstance(d, dict) and "feature_count" in d,
                d.get("error") or f"feat={d.get('feature_count')}")
    except Exception as e:
        _record("derive.select_by_location(point+radius)", False, repr(e))

    # ---- checkpoint / edit_field (wrap under a checkpoint) ----
    try:
        d = await _call(mcp, "checkpoint", {"op": "create", "name": "t1"})
        _record("checkpoint.create", isinstance(d, dict) and not _is_error(d),
                str(d)[:80])
    except Exception as e:
        _record("checkpoint.create", False, repr(e))

    try:
        d = await _call(mcp, "edit_field", {
            "op": "add", "layer": "d_top",
            "name": "area_m2", "expr": "ST_Area(geom)",
        })
        _record("edit_field.add", not _is_error(d),
                "hint=" + ("yes" if d.get("hint") else "no"))
    except Exception as e:
        _record("edit_field.add", False, repr(e))

    try:
        d = await _call(mcp, "edit_field", {
            "op": "update", "layer": "d_top",
            "name": "area_m2", "expr": "area_m2 / 1e6",
        })
        _record("edit_field.update", not _is_error(d), d.get("error") or "ok")
    except Exception as e:
        _record("edit_field.update", False, repr(e))

    try:
        d = await _call(mcp, "edit_field", {
            "op": "classify", "layer": "d_top", "name": "size_band",
            "rules": [
                {"when": "area_m2 < 1", "then": "small"},
                {"when": "area_m2 >= 1", "then": "large"},
            ],
            "default": "unknown",
        })
        _record("edit_field.classify", not _is_error(d), d.get("error") or "ok")
    except Exception as e:
        _record("edit_field.classify", False, repr(e))

    try:
        d = await _call(mcp, "edit_field", {
            "op": "annotate", "layer": "d_top", "key_column": "desokod",
            "values": {"0180C1010": {"note": "stress_test"}},
            "dry_run": True,
        })
        _record("edit_field.annotate(dry_run)", d.get("dry_run") is True,
                f"matched={d.get('keys_matched')}")
    except Exception as e:
        _record("edit_field.annotate(dry_run)", False, repr(e))

    try:
        d = await _call(mcp, "edit_field", {
            "op": "drop", "layer": "d_top", "name": "size_band",
        })
        _record("edit_field.drop", not _is_error(d), d.get("error") or "ok")
    except Exception as e:
        _record("edit_field.drop", False, repr(e))

    try:
        d = await _call(mcp, "checkpoint", {"op": "rollback", "name": "t1"})
        _record("checkpoint.rollback", d.get("status") == "rolled_back",
                f"restored={d.get('restored')}")
    except Exception as e:
        _record("checkpoint.rollback", False, repr(e))

    # Fresh checkpoint + commit cycle
    try:
        await _call(mcp, "checkpoint", {"op": "create", "name": "t2"})
        await _call(mcp, "edit_field", {"op": "add", "layer": "d_top",
                                         "name": "marker", "expr": "1"})
        d = await _call(mcp, "checkpoint", {"op": "commit", "name": "t2"})
        _record("checkpoint.commit", d.get("status") == "committed",
                f"committed={d.get('committed') or d}")
    except Exception as e:
        _record("checkpoint.commit", False, repr(e))

    # ---- inspect ----
    try:
        d = await _call(mcp, "inspect", {"op": "layers"})
        _record("inspect.layers", isinstance(d, dict) and d.get("n_layers", 0) > 0,
                f"n_layers={d.get('n_layers')}")
    except Exception as e:
        _record("inspect.layers", False, repr(e))

    try:
        d = await _call(mcp, "inspect", {"op": "rows", "layer": "d_top", "n": 3})
        _record("inspect.rows", d.get("rows_shown") == 3,
                f"shown={d.get('rows_shown')}/{d.get('rows_total')}")
    except Exception as e:
        _record("inspect.rows", False, repr(e))

    # Feature: inspect(op="rows", include_rowid=True)
    try:
        d = await _call(mcp, "inspect", {"op": "rows", "layer": "d_top",
                                          "n": 3, "include_rowid": True})
        has_rowid = "rowid" in (d.get("table_md") or "")
        _record("inspect.rows(include_rowid)", has_rowid,
                "rowid column present" if has_rowid else "rowid missing")
    except Exception as e:
        _record("inspect.rows(include_rowid)", False, repr(e))

    try:
        d = await _call(mcp, "inspect", {
            "op": "batch", "layer": "d_top", "batch_size": 3,
            "columns": ["desokod"],
        })
        cursor = d.get("next_cursor")
        first_ok = bool(d.get("rows"))
        # Continue
        d2 = await _call(mcp, "inspect", {"op": "batch", "cursor": cursor})
        second_ok = d2.get("exhausted") is not None
        _record("inspect.batch(cursor round-trip)", first_ok and second_ok,
                f"first_rows={len(d.get('rows') or [])} second_exhausted={d2.get('exhausted')}")
    except Exception as e:
        _record("inspect.batch(cursor round-trip)", False, repr(e))

    try:
        d = await _call(mcp, "inspect", {"op": "at",
            "points": [{"id": "p1", "x_3011": 153844.0, "y_3011": 6578679.0}],
            "radius_m": 1000, "per_layer_limit": 2})
        _record("inspect.at", isinstance(d, dict) and len(d.get("points", [])) == 1,
                f"layers_considered={d.get('layers_considered')}")
    except Exception as e:
        _record("inspect.at", False, repr(e))

    # ---- layer ----
    try:
        d = await _call(mcp, "layer", {"op": "show", "layers": ["d_top"], "title": "Stress"})
        _record("layer.show", [l["name"] for l in d.get("visible_layers", [])] == ["d_top"],
                d.get("viewer_url"))
    except Exception as e:
        _record("layer.show", False, repr(e))

    try:
        d = await _call(mcp, "layer", {"op": "set_notes", "name": "d_top", "notes": "stress test"})
        _record("layer.set_notes", not _is_error(d), d.get("error") or "ok")
    except Exception as e:
        _record("layer.set_notes", False, repr(e))

    try:
        d = await _call(mcp, "layer", {"op": "hide"})
        _record("layer.hide(all)", not _is_error(d), d.get("error") or "ok")
    except Exception as e:
        _record("layer.hide(all)", False, repr(e))

    try:
        d = await _call(mcp, "layer", {"op": "rename", "name": "d_top", "new_name": "d_top_renamed"})
        _record("layer.rename", isinstance(d, dict) and d.get("renamed"), d.get("error") or "ok")
    except Exception as e:
        _record("layer.rename", False, repr(e))

    try:
        d = await _call(mcp, "layer", {"op": "drop", "name": "d_clip"})
        _record("layer.drop", not _is_error(d), d.get("error") or "ok")
    except Exception as e:
        _record("layer.drop", False, repr(e))

    # ---- export ----
    try:
        d = await _call(mcp, "export", {"layers": "deso_2025", "format": "gpkg"})
        _record("export.single.gpkg", bool(d.get("url")), d.get("url"))
    except Exception as e:
        _record("export.single.gpkg", False, repr(e))

    try:
        d = await _call(mcp, "export", {
            "layers": ["deso_2025", "deso_2018"],
            "format": "gpkg",
        })
        _record("export.multi.gpkg", bool(d.get("url") or d.get("files")),
                f"url={d.get('url')} files={len(d.get('files', []))}")
    except Exception as e:
        _record("export.multi.gpkg", False, repr(e))

    try:
        d = await _call(mcp, "export", {"layers": "deso_2025", "format": "geojson", "cite": True})
        has_cite = bool(d.get("citations_markdown") or d.get("citation_md"))
        _record("export.single.geojson(cite)",
                bool(d.get("url")) and has_cite,
                f"cite_keys={[k for k in d if 'cit' in k.lower()]}")
    except Exception as e:
        _record("export.single.geojson(cite)", False, repr(e))

    try:
        d = await _call(mcp, "export", {
            "layers": ["deso_2025", "deso_2018"],
            "format": "geojson", "merge_geojson": True,
        })
        _record("export.multi.geojson(merge)",
                bool(d.get("url") or d.get("files")),
                f"files={len(d.get('files', []))}")
    except Exception as e:
        _record("export.multi.geojson(merge)", False, repr(e))

    try:
        d = await _call(mcp, "export", {"layers": "deso_2025", "format": "csv"})
        _record("export.csv", bool(d.get("url")), d.get("url"))
    except Exception as e:
        _record("export.csv", False, repr(e))

    try:
        d = await _call(mcp, "export", {"layers": "deso_2025", "format": "parquet"})
        _record("export.parquet", bool(d.get("url")), d.get("url"))
    except Exception as e:
        _record("export.parquet", False, repr(e))

    try:
        d = await _call(mcp, "render_map", {
            "layers": ["deso_top5"], "width_px": 800, "height_px": 600,
        })
        _record("render_map", d.get("format") == "png" and bool(d.get("url")),
                f"size={d.get('size_bytes')}")
    except Exception as e:
        _record("render_map", False, repr(e))

    try:
        d = await _call(mcp, "render_map", {"layers": []})
        _record("render_map(missing_arg)", _is_error(d), d.get("error"))
    except Exception as e:
        _record("render_map(missing_arg)", False, repr(e))

    # ---- sources ----
    try:
        d = await _call(mcp, "sources", {})
        ok = isinstance(d, str) and len(d) > 0
        _record("sources(all)", ok, f"chars={len(d) if isinstance(d, str) else 0}")
    except Exception as e:
        _record("sources(all)", False, repr(e))

    # Regression: sources() on an empty session must return a helpful
    # prompt, not an empty string (bug fixed 2026-04-22).
    try:
        from geodata_mcp.session import REGISTRY
        # A throwaway session with zero layers for this test only.
        REGISTRY.get_or_create("stress_test_empty_session_probe")
        import geodata_mcp.server as _srv  # type: ignore
        _orig = _srv._session
        _srv._session = lambda ctx, _r=REGISTRY: _r.get_or_create(
            "stress_test_empty_session_probe"
        )
        try:
            d = await _call(mcp, "sources", {})
        finally:
            _srv._session = _orig
        ok = isinstance(d, str) and "No layers" in d
        _record("sources(empty session)", ok, f"excerpt={(d or '')[:60]!r}")
    except Exception as e:
        _record("sources(empty session)", False, repr(e))

    try:
        d = await _call(mcp, "sources", {"layer": "deso_2025"})
        ok = isinstance(d, str) and "deso_2025" in d
        _record("sources(layer=deso_2025)", ok, f"chars={len(d) if isinstance(d, str) else 0}")
    except Exception as e:
        _record("sources(layer=deso_2025)", False, repr(e))

    # ---- error paths (extra) ----
    try:
        d = await _call(mcp, "inspect", {"op": "rows", "layer": "nosuch_layer"})
        _record("inspect.rows(unknown_layer)", _is_error(d), d.get("error"))
    except Exception as e:
        _record("inspect.rows(unknown_layer)", False, repr(e))

    try:
        # too_many_points cap is 500
        d = await _call(mcp, "inspect", {
            "op": "at",
            "points": [{"id": i, "x_3011": 0, "y_3011": 0} for i in range(501)],
        })
        _record("inspect.at(too_many_points)",
                _is_error(d) and d["error"] == "too_many_points", d.get("error"))
    except Exception as e:
        _record("inspect.at(too_many_points)", False, repr(e))

    try:
        # unsupported_operation — pass an impossible op string.
        # FastMCP schema validation will reject unknown Literal values before
        # reaching the tool body, so this is mainly a documentation test.
        d = await _call(mcp, "checkpoint", {"op": "definitely_not_a_real_op", "name": "x"})
        _record("checkpoint(unsupported op)",
                _is_error(d) or "validation" in str(d).lower(), str(d)[:80])
    except Exception as e:
        # Schema rejection raises at the tool layer — also counts as success.
        _record("checkpoint(unsupported op)", True, f"schema rejection: {type(e).__name__}")


async def run_end_to_end(mcp) -> None:
    print("\n=== End-to-end: Tekniska nämndhuset income ===")
    try:
        g = await _call(mcp, "geocode", {"op": "forward",
                                         "name": "Tekniska nämndhuset", "limit": 1})
        if not g.get("matches"):
            _record("e2e.geocode", False, "no match")
            return
        x, y = g["matches"][0]["x_3011"], g["matches"][0]["y_3011"]
        _record("e2e.geocode", True, f"x={x:.0f} y={y:.0f}")

        d = await _call(mcp, "load", {
            "op": "catalog",
            "dataset_ids": ["scb_income_structure"],
        })
        _record("e2e.load(income)", d.get("n_loaded") == 1,
                f"n_loaded={d.get('n_loaded')}")

        sql = f"""
            WITH here AS (
              SELECT desokod FROM deso_2025
              WHERE ST_Contains(geom, ST_Point({x}, {y}))
            )
            SELECT år, value AS mean_tkr
            FROM scb_income_structure i JOIN here h ON i.desokod_2025 = h.desokod
            WHERE tabellinnehåll='Medelvärde för samtliga, tkr'
              AND kön='totalt'
              AND inkomstkomponent='nettoinkomst'
              AND value IS NOT NULL
            ORDER BY år DESC LIMIT 3
        """
        d = await _call(mcp, "execute_sql", {"sql": sql})
        rows = d.get("rows") or []
        _record("e2e.execute_sql(join)", bool(rows),
                f"rows={rows}")

        d = await _call(mcp, "sources", {"layer": "deso_2025"})
        text = d if isinstance(d, str) else ""
        _record("e2e.sources", "SBK" in text or "SCB" in text or "deso" in text.lower(),
                f"chars={len(text)}")

    except Exception:
        traceback.print_exc()
        _record("e2e.flow", False, "exception; see traceback above")


async def main() -> int:
    print("Loading MCP server...")
    # Wipe any state from a previous run of this script BEFORE importing
    # the server (which opens the DuckDB handle). Otherwise stale tables
    # collide with load() and mask real failures as "Table already exists".
    import pathlib, os
    from pathlib import Path
    session_dir = Path(__file__).resolve().parents[1] / ".duckdb" / "sessions"
    for suffix in ("default.duckdb", "default.meta.json", "default.audit.jsonl"):
        p = session_dir / suffix
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    from geodata_mcp.server import mcp  # type: ignore
    await run_coverage(mcp)
    await run_end_to_end(mcp)

    print("\n=== Summary ===")
    failures = [r for r in _results if not r["ok"]]
    passes = len(_results) - len(failures)
    print(f"{passes}/{len(_results)} passed, {len(failures)} failed")
    if failures:
        print("\nFailures:")
        for r in failures:
            print(f"  - {r['name']}: {r['detail']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
