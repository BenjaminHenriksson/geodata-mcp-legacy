"""Spatial inspection + provenance walker: sources, inspect_location,
inspect_locations, reverse_geocode."""
from __future__ import annotations

from datetime import datetime

from ..loader import LoadError as OpError, _quote_ident
from ..session import Operation, Session


# ---------- sources / provenance walker ----------


def _ancestry(session: Session, layer: str) -> list[str]:
    """Return layers in BFS order from `layer` back through parents. `layer` first."""
    seen = {layer}
    order = [layer]
    queue = [layer]
    while queue:
        cur = queue.pop(0)
        m = session.layers.get(cur)
        if m is None:
            continue
        for p in m.parent_layers:
            if p in seen:
                continue
            seen.add(p)
            order.append(p)
            queue.append(p)
    return order


def sources(session: Session, layer: str | None = None) -> str:
    """Markdown report of the provenance chain and operations applied."""
    targets = [layer] if layer else list(session.layers)
    if layer and layer not in session.layers:
        return (f"[server-side error from geodata-mcp server] unknown layer "
                f"'{layer}' in this session. Available: {list(session.layers)}")
    out: list[str] = []
    for lname in targets:
        m = session.layers[lname]
        ancestors = _ancestry(session, lname)
        ops = [op for op in session.history if op.result_layer in ancestors]
        out.append(f"### Layer: `{lname}`  ({m.feature_count} features, created by `{m.created_by}`)")
        if m.parent_layers:
            out.append(f"Derived from: {', '.join(f'`{p}`' for p in m.parent_layers)}")
        out.append("")
        out.append("**Operations applied** (root → result):")
        for op in ops:
            arg_str = ", ".join(f"{k}={v!r}" for k, v in op.args.items() if v is not None)
            out.append(f"- `{op.tool}({arg_str})` → `{op.result_layer}`: {op.summary}")
        out.append("")
        out.append("**Sources** (deduplicated):")
        if not m.provenance:
            out.append("- (no source references)")
        for i, s in enumerate(m.provenance, 1):
            line = f"{i}. "
            if s.llm_sourced:
                line += f"**LLM-sourced**: {s.source_name}"
                if s.llm_source_description:
                    line += f" — _{s.llm_source_description}_"
            else:
                line += f"**{s.source_name}** (`{s.dataset_id}`)"
            out.append(line)
            if s.publisher: out.append(f"   - Publisher: {s.publisher}")
            if s.license: out.append(f"   - License: {s.license}")
            if s.url: out.append(f"   - URL: {s.url}")
            if s.file_path: out.append(f"   - File: `{s.file_path}`")
            if s.retrieved: out.append(f"   - Retrieved: {s.retrieved}")
        if m.column_provenance:
            out.append("")
            out.append("**Column attribution** (columns written in-session, "
                       "distinct from the layer's loaded sources above):")
            for col in sorted(m.column_provenance):
                cp = m.column_provenance[col]
                author = cp.get("authored_by", "?")
                tool = cp.get("tool", "?")
                at = cp.get("at", "")
                line = f"- `{col}` — {author} via `{tool}` at {at}"
                if cp.get("model"):
                    line += f" (model: {cp['model']})"
                if cp.get("expr"):
                    line += f"\n    expr: `{cp['expr']}`"
                out.append(line)
        out.append("")
    return "\n".join(out)


# ---------- inspect_location / inspect_locations / reverse_geocode ----------


def _inspect_one_point(
    session: Session, x: float, y: float, radius_m: float,
    targets: list[str], columns: list[str] | None, per_layer_limit: int,
) -> tuple[list[dict], list[str]]:
    """Run the per-layer ST_DWithin query for a single point. Returns
    (results, unknown_layers)."""
    results = []
    unknown = []
    for lname in targets:
        meta = session.layers.get(lname)
        if meta is None:
            unknown.append(lname)
            continue
        geom_col = meta.attributes.get("__geom_col__") or ""
        if not geom_col:
            continue
        qlayer = _quote_ident(lname)
        qgeom = _quote_ident(geom_col)
        declared = [c for c in meta.attributes
                    if not c.startswith("__") and c != geom_col]
        if columns:
            # Intersect requested cols with declared; silently drop unknown.
            attr_cols = [c for c in columns if c in declared]
        else:
            attr_cols = declared
        attr_select = ", ".join(_quote_ident(c) for c in attr_cols) or "NULL AS _"
        sql = (
            f"SELECT {attr_select}, "
            f"  ST_Distance({qgeom}, ST_Point(?, ?)) AS _dist_m "
            f"FROM {qlayer} "
            f"WHERE ST_DWithin({qgeom}, ST_Point(?, ?), ?) "
            f"ORDER BY _dist_m ASC "
            f"LIMIT {per_layer_limit}"
        )
        try:
            rows = session.conn.execute(
                sql, [x, y, x, y, radius_m]
            ).fetchall()
            col_names = [d[0] for d in session.conn.description]
        except Exception as e:
            results.append({"layer": lname, "error": f"{type(e).__name__}: {e}"})
            continue
        if not rows:
            continue
        hits = [dict(zip(col_names, r)) for r in rows]
        for h in hits:
            if "_dist_m" in h and h["_dist_m"] is not None:
                h["distance_m"] = round(float(h.pop("_dist_m")), 2)
            # Drop NULL attributes to keep payload small.
            for k in list(h.keys()):
                if h[k] is None:
                    h.pop(k)
        results.append({
            "layer": lname,
            "geometry_type": meta.geometry_type,
            "features": hits,
        })
    return results, unknown


def inspect_location(
    session: Session,
    x_3011: float,
    y_3011: float,
    radius_m: float = 100.0,
    layers: list[str] | None = None,
    columns: list[str] | None = None,
    per_layer_limit: int = 3,
) -> dict:
    """One-shot "what's here?" across multiple layers. See the tool wrapper
    for full description."""
    per_layer_limit = max(1, min(int(per_layer_limit), 25))
    radius_m = max(0.0, float(radius_m))

    geom_layers = [n for n, m in session.layers.items()
                   if m.attributes.get("__geom_col__")]
    if layers:
        targets = layers
    else:
        targets = geom_layers

    payload = {
        "query": {"x_3011": x_3011, "y_3011": y_3011, "radius_m": radius_m},
        "layers_considered": len(targets),
    }
    if not session.layers:
        payload.update({"results": [],
                        "hint": "no layers loaded in this session; call load() or load_many() first"})
        return payload
    if not geom_layers:
        payload.update({"results": [],
                        "hint": "no layers with geometry in this session"})
        return payload

    results, unknown = _inspect_one_point(
        session, x_3011, y_3011, radius_m,
        targets, columns, per_layer_limit,
    )
    session.log(Operation(
        tool="inspect_location",
        args={"x_3011": x_3011, "y_3011": y_3011, "radius_m": radius_m,
              "layers": targets},
        result_layer=None,
        summary=f"{sum(len(r.get('features', [])) for r in results)} features across {len(results)} layers",
        at=datetime.utcnow(),
    ))
    payload["results"] = results
    if unknown:
        payload["unknown_layers"] = unknown
        payload["warning"] = (
            f"layers {unknown} not loaded in this session — they were skipped. "
            "Call load() / load_many() first."
        )
    if all(not r.get("features") for r in results) and not unknown:
        payload["hint"] = (
            f"no features within {radius_m} m of ({x_3011}, {y_3011}) across "
            f"{len(results)} loaded layers. Try a larger radius, a different "
            "point, or load more layers."
        )
    return payload


def inspect_locations(
    session: Session,
    points: list[dict],
    radius_m: float = 100.0,
    layers: list[str] | None = None,
    columns: list[str] | None = None,
    per_layer_limit: int = 3,
) -> dict:
    """Batch spatial lookup: for each point in `points`, return the same
    per-layer nearest-feature hits as `inspect_location`."""
    per_layer_limit = max(1, min(int(per_layer_limit), 25))
    radius_m = max(0.0, float(radius_m))

    geom_layers = [n for n, m in session.layers.items()
                   if m.attributes.get("__geom_col__")]
    targets = layers if layers else geom_layers

    overall_unknown: set[str] = set()
    out_points = []
    for i, p in enumerate(points):
        x = p.get("x_3011")
        y = p.get("y_3011")
        pid = p.get("id", i)
        if x is None or y is None:
            out_points.append({"id": pid, "error": "missing x_3011/y_3011"})
            continue
        results, unknown = _inspect_one_point(
            session, float(x), float(y), radius_m,
            targets, columns, per_layer_limit,
        )
        overall_unknown.update(unknown)
        out_points.append({
            "id": pid,
            "x_3011": x, "y_3011": y,
            "results": results,
        })
    session.log(Operation(
        tool="inspect_locations",
        args={"n_points": len(points), "radius_m": radius_m,
              "layers": targets},
        result_layer=None,
        summary=f"{len(points)} points × {len(targets)} layers",
        at=datetime.utcnow(),
    ))
    payload = {
        "n_points": len(points),
        "layers_considered": len(targets),
        "points": out_points,
    }
    if overall_unknown:
        payload["unknown_layers"] = sorted(overall_unknown)
        payload["warning"] = (
            f"layers {sorted(overall_unknown)} not loaded — skipped for every point."
        )
    if not geom_layers:
        payload["hint"] = "no layers with geometry in this session"
    return payload


def reverse_geocode(
    session: Session,
    x_3011: float,
    y_3011: float,
    admin_gpkg_path: str,
) -> dict:
    """Given a coordinate in EPSG:3011, return the administrative areas
    (Stadsdel, Distrikt, Stadsdelsnämndsområde, Kvarter, Kommun) from SBK's
    Adm_area that contain the point. Intended to kill the temptation to
    guess neighborhoods from raw coordinates.

    Args:
        x_3011, y_3011: query point in EPSG:3011.
        admin_gpkg_path: path to Adm_area.gpkg (injected by the tool wrapper).

    Returns a dict with the containing polygons grouped by KATEGORI.
    """
    try:
        rows = session.conn.execute(
            """
            SELECT KATEGORI, NAMN,
                   ROUND(ST_Area(geom), 0) AS area_m2
            FROM ST_Read(?)
            WHERE ST_Contains(geom, ST_Point(?, ?))
            ORDER BY area_m2 ASC
            """,
            [admin_gpkg_path, float(x_3011), float(y_3011)],
        ).fetchall()
    except Exception as e:
        raise OpError(f"reverse_geocode failed: {type(e).__name__}: {e}")

    containing = []
    for kategori, namn, area in rows:
        containing.append({
            "kategori": kategori,
            "namn": namn,
            "area_m2": int(area) if area is not None else None,
        })
    # Group by kategori for convenience.
    by_kategori: dict[str, list[dict]] = {}
    for r in containing:
        by_kategori.setdefault(r["kategori"] or "_unknown", []).append(
            {"namn": r["namn"], "area_m2": r["area_m2"]}
        )

    payload = {
        "query": {"x_3011": x_3011, "y_3011": y_3011},
        "containing": containing,
        "by_kategori": by_kategori,
    }
    if not containing:
        payload["warning"] = (
            f"no SBK admin polygons contain ({x_3011}, {y_3011}) — either the "
            "point is outside Stockholm kommun or outside the SBK coverage "
            "area. Do NOT guess the neighborhood; state that the point is "
            "unidentifiable from the available data."
        )
    return payload
