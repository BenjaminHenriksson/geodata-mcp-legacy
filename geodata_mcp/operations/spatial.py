"""Spatial overlay / geometry ops: clip, buffer, centroid, dissolve,
convex_hull, intersect, select_by_location."""
from __future__ import annotations

from datetime import datetime

from ..loader import LoadError as OpError, _quote_ident
from ..session import LayerMeta, Operation, Session
from ._util import _geom_col, _register_result, _require_layer


def _clip_geom_expr(session: Session, by_layer: str) -> tuple[str, str]:
    """Build a SQL scalar subquery that yields the union of `by_layer`'s geometry
    as a single (Multi)Polygon. Also returns the geom column name for reference."""
    m = _require_layer(session, by_layer)
    g = _geom_col(m)
    expr = (f"(SELECT ST_Union_Agg({_quote_ident(g)}) "
            f"FROM {_quote_ident(by_layer)})")
    return expr, g


def spatial_clip(
    session: Session, source_layer: str, by_layer: str,
    *, result_name: str | None = None,
) -> LayerMeta:
    src = _require_layer(session, source_layer)
    by = _require_layer(session, by_layer)
    src_g = _geom_col(src)
    clip_expr, _ = _clip_geom_expr(session, by_layer)

    new_name = session.unique_layer_name(result_name or f"{source_layer}_clipped")
    cols = [c for c in src.attributes if not c.startswith("__") and c != src_g]
    attr_select = ", ".join(f"s.{_quote_ident(c)}" for c in cols)
    sql = (
        f"CREATE TABLE {_quote_ident(new_name)} AS "
        f"SELECT {attr_select + ',' if attr_select else ''} "
        f"ST_Intersection(s.{_quote_ident(src_g)}, {clip_expr}) AS {_quote_ident(src_g)} "
        f"FROM {_quote_ident(source_layer)} s "
        f"WHERE ST_Intersects(s.{_quote_ident(src_g)}, {clip_expr})"
    )
    session.conn.execute(sql)
    meta = _register_result(session, new_name, [source_layer, by_layer], created_by="spatial.clip")
    session.log(Operation(
        tool="spatial.clip", args={"layer": source_layer, "by_layer": by_layer},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


def spatial_buffer(
    session: Session, source_layer: str, distance_m: float,
    *, result_name: str | None = None,
) -> LayerMeta:
    src = _require_layer(session, source_layer)
    src_g = _geom_col(src)
    new_name = session.unique_layer_name(result_name or f"{source_layer}_buffer{int(distance_m)}")
    cols = [c for c in src.attributes if not c.startswith("__") and c != src_g]
    attr_select = ", ".join(_quote_ident(c) for c in cols)
    sql = (
        f"CREATE TABLE {_quote_ident(new_name)} AS "
        f"SELECT {attr_select + ',' if attr_select else ''} "
        f"ST_Buffer({_quote_ident(src_g)}, {float(distance_m)}) AS {_quote_ident(src_g)} "
        f"FROM {_quote_ident(source_layer)}"
    )
    session.conn.execute(sql)
    meta = _register_result(session, new_name, [source_layer], created_by="spatial.buffer")
    session.log(Operation(
        tool="spatial.buffer", args={"layer": source_layer, "distance_m": distance_m},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


def spatial_centroid(
    session: Session, source_layer: str,
    *, result_name: str | None = None,
) -> LayerMeta:
    src = _require_layer(session, source_layer)
    src_g = _geom_col(src)
    new_name = session.unique_layer_name(result_name or f"{source_layer}_centroid")
    cols = [c for c in src.attributes if not c.startswith("__") and c != src_g]
    attr_select = ", ".join(_quote_ident(c) for c in cols)
    sql = (
        f"CREATE TABLE {_quote_ident(new_name)} AS "
        f"SELECT {attr_select + ',' if attr_select else ''} "
        f"ST_Centroid({_quote_ident(src_g)}) AS {_quote_ident(src_g)} "
        f"FROM {_quote_ident(source_layer)}"
    )
    session.conn.execute(sql)
    meta = _register_result(session, new_name, [source_layer], created_by="spatial.centroid")
    session.log(Operation(
        tool="spatial.centroid", args={"layer": source_layer},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


def spatial_dissolve(
    session: Session, source_layer: str,
    by_columns: list[str] | None = None,
    *, result_name: str | None = None,
) -> LayerMeta:
    """Union geometries, optionally grouped by attribute columns.

    With no by_columns: emits 1 feature — the union of all.
    With by_columns: emits 1 feature per distinct combination of those columns.
    """
    src = _require_layer(session, source_layer)
    src_g = _geom_col(src)
    new_name = session.unique_layer_name(result_name or f"{source_layer}_dissolved")
    if by_columns:
        for c in by_columns:
            if c not in src.attributes:
                raise OpError(f"column '{c}' not in layer '{source_layer}'")
        group_cols = ", ".join(_quote_ident(c) for c in by_columns)
        select_cols = ", ".join(_quote_ident(c) for c in by_columns)
        sql = (
            f"CREATE TABLE {_quote_ident(new_name)} AS "
            f"SELECT {select_cols}, "
            f"       ST_Union_Agg({_quote_ident(src_g)}) AS {_quote_ident(src_g)}, "
            f"       COUNT(*) AS feature_count "
            f"FROM {_quote_ident(source_layer)} "
            f"GROUP BY {group_cols}"
        )
    else:
        sql = (
            f"CREATE TABLE {_quote_ident(new_name)} AS "
            f"SELECT COUNT(*) AS feature_count, "
            f"       ST_Union_Agg({_quote_ident(src_g)}) AS {_quote_ident(src_g)} "
            f"FROM {_quote_ident(source_layer)}"
        )
    session.conn.execute(sql)
    meta = _register_result(session, new_name, [source_layer], created_by="spatial.dissolve")
    session.log(Operation(
        tool="spatial.dissolve",
        args={"layer": source_layer, "by_columns": by_columns},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


def spatial_convex_hull(
    session: Session, source_layer: str,
    *, aggregate: bool = False, result_name: str | None = None,
) -> LayerMeta:
    """Per-feature convex hull by default; with aggregate=True, emit a single
    feature: the convex hull of the union of all geometries."""
    src = _require_layer(session, source_layer)
    src_g = _geom_col(src)
    new_name = session.unique_layer_name(result_name or f"{source_layer}_hull")
    if aggregate:
        sql = (
            f"CREATE TABLE {_quote_ident(new_name)} AS "
            f"SELECT COUNT(*) AS source_feature_count, "
            f"       ST_ConvexHull(ST_Union_Agg({_quote_ident(src_g)})) AS {_quote_ident(src_g)} "
            f"FROM {_quote_ident(source_layer)}"
        )
    else:
        cols = [c for c in src.attributes if not c.startswith("__") and c != src_g]
        attr_select = ", ".join(_quote_ident(c) for c in cols)
        sql = (
            f"CREATE TABLE {_quote_ident(new_name)} AS "
            f"SELECT {attr_select + ',' if attr_select else ''} "
            f"ST_ConvexHull({_quote_ident(src_g)}) AS {_quote_ident(src_g)} "
            f"FROM {_quote_ident(source_layer)}"
        )
    session.conn.execute(sql)
    meta = _register_result(session, new_name, [source_layer], created_by="spatial.convex_hull")
    session.log(Operation(
        tool="spatial.convex_hull",
        args={"layer": source_layer, "aggregate": aggregate},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


def spatial_intersect(
    session: Session, a_layer: str, b_layer: str,
    *, result_name: str | None = None,
) -> LayerMeta:
    """**Geometric** intersection (overlay): for every pair (a, b) where the
    geometries intersect, emit one row with a's attributes (prefixed `a_`),
    b's attributes (prefixed `b_`), and the geometry = `ST_Intersection(a, b)`.

    NOTE: if you want "rows of A that intersect anything in B" with A's
    geometry unchanged, use `spatial(operation='select_by_location', ...)`
    instead — that's the more common spatial-join pattern.
    """
    a = _require_layer(session, a_layer)
    b = _require_layer(session, b_layer)
    a_g, b_g = _geom_col(a), _geom_col(b)
    new_name = session.unique_layer_name(result_name or f"{a_layer}_x_{b_layer}")
    a_cols = [c for c in a.attributes if not c.startswith("__") and c != a_g]
    b_cols = [c for c in b.attributes if not c.startswith("__") and c != b_g]
    a_select = ", ".join(f"a.{_quote_ident(c)} AS {_quote_ident('a_' + c)}" for c in a_cols)
    b_select = ", ".join(f"b.{_quote_ident(c)} AS {_quote_ident('b_' + c)}" for c in b_cols)
    geom_col_out = "geom"
    sql = (
        f"CREATE TABLE {_quote_ident(new_name)} AS "
        f"SELECT {a_select}{',' if a_select else ''} "
        f"       {b_select}{',' if b_select else ''} "
        f"       ST_Intersection(a.{_quote_ident(a_g)}, b.{_quote_ident(b_g)}) AS {_quote_ident(geom_col_out)} "
        f"FROM {_quote_ident(a_layer)} a "
        f"JOIN {_quote_ident(b_layer)} b "
        f"  ON ST_Intersects(a.{_quote_ident(a_g)}, b.{_quote_ident(b_g)}) "
        f"WHERE NOT ST_IsEmpty(ST_Intersection(a.{_quote_ident(a_g)}, b.{_quote_ident(b_g)}))"
    )
    session.conn.execute(sql)
    meta = _register_result(session, new_name, [a_layer, b_layer], created_by="spatial.intersect")
    session.log(Operation(
        tool="spatial.intersect",
        args={"a_layer": a_layer, "b_layer": b_layer},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


def spatial_select_by_location(
    session: Session, source_layer: str,
    by_layer: str | None = None,
    *, predicate: str = "intersects", distance_m: float | None = None,
    center_3011: tuple[float, float] | list[float] | None = None,
    result_name: str | None = None,
) -> LayerMeta:
    """Select features of `source_layer` whose geometry relates to ANY feature
    in `by_layer` by the given predicate. Source geometry and attributes are
    preserved unchanged — this is the spatial equivalent of a `WHERE` clause.

    Two modes:
      - **by_layer** (default): relate to any feature in another session layer.
      - **center_3011 + distance_m** (point+radius): relate to a literal
        EPSG:3011 point. Implies `predicate='dwithin'`. Saves the
        "inject a 1-row point layer first" dance.

    Predicates (by_layer mode):
      - intersects: ST_Intersects (default) — touch, overlap, contain, equal
      - within:     ST_Within                — source fully inside something in by_layer
      - contains:   ST_Contains              — source fully contains something in by_layer
      - dwithin:    ST_DWithin with `distance_m` metres (EPSG:3011)

    Distinct from `spatial_intersect`, which returns the **geometric**
    intersection (A ∩ B) and typically changes geometry kind.
    """
    src = _require_layer(session, source_layer)
    src_g = _geom_col(src)

    # Point+radius mode — no by_layer, literal geometry in the predicate.
    if center_3011 is not None:
        if by_layer is not None:
            raise OpError(
                "pass either by_layer or center_3011, not both"
            )
        if distance_m is None:
            raise OpError("center_3011 requires distance_m")
        try:
            cx, cy = float(center_3011[0]), float(center_3011[1])
        except (TypeError, IndexError, ValueError) as e:
            raise OpError(f"center_3011 must be [x, y] floats: {e}")
        cond = (f"ST_DWithin(s.{_quote_ident(src_g)}, "
                f"ST_Point({cx}, {cy}), {float(distance_m)})")
        new_name = session.unique_layer_name(
            result_name or f"{source_layer}_within_{int(distance_m)}m"
        )
        qnew = _quote_ident(new_name)
        qsrc = _quote_ident(source_layer)
        sql = (
            f"CREATE TABLE {qnew} AS "
            f"SELECT s.* FROM {qsrc} s WHERE {cond}"
        )
        session.conn.execute(sql)
        meta = _register_result(
            session, new_name, [source_layer],
            created_by="spatial.select_by_location(point+radius)",
        )
        session.log(Operation(
            tool="spatial.select_by_location",
            args={"layer": source_layer, "center_3011": [cx, cy],
                  "distance_m": distance_m},
            result_layer=new_name, summary=f"{meta.feature_count} features",
            at=datetime.utcnow(),
        ))
        return meta

    # by_layer mode — existing path.
    if by_layer is None:
        raise OpError(
            "select_by_location requires either `by_layer` or "
            "`center_3011`+`distance_m`"
        )
    by = _require_layer(session, by_layer)
    by_g = _geom_col(by)

    pred = predicate.lower()
    if pred == "dwithin":
        if distance_m is None:
            raise OpError("predicate='dwithin' requires distance_m")
        cond = (f"ST_DWithin(s.{_quote_ident(src_g)}, b.{_quote_ident(by_g)}, "
                f"{float(distance_m)})")
    elif pred == "intersects":
        cond = f"ST_Intersects(s.{_quote_ident(src_g)}, b.{_quote_ident(by_g)})"
    elif pred == "within":
        cond = f"ST_Within(s.{_quote_ident(src_g)}, b.{_quote_ident(by_g)})"
    elif pred == "contains":
        cond = f"ST_Contains(s.{_quote_ident(src_g)}, b.{_quote_ident(by_g)})"
    else:
        raise OpError(
            f"unsupported predicate '{predicate}'. "
            "Supported: intersects, within, contains, dwithin."
        )

    new_name = session.unique_layer_name(
        result_name or f"{source_layer}_{pred}_{by_layer}"
    )
    qnew = _quote_ident(new_name)
    qsrc = _quote_ident(source_layer)
    qby = _quote_ident(by_layer)
    sql = (
        f"CREATE TABLE {qnew} AS "
        f"SELECT DISTINCT s.* FROM {qsrc} s "
        f"WHERE EXISTS (SELECT 1 FROM {qby} b WHERE {cond})"
    )
    session.conn.execute(sql)
    meta = _register_result(
        session, new_name, [source_layer, by_layer],
        created_by=f"spatial.select_by_location({pred})",
    )
    session.log(Operation(
        tool="spatial.select_by_location",
        args={"layer": source_layer, "by_layer": by_layer,
              "predicate": pred, "distance_m": distance_m},
        result_layer=new_name, summary=f"{meta.feature_count} features",
        at=datetime.utcnow(),
    ))
    return meta


SPATIAL_OPS = {
    "clip": ("spatial_clip", ("layer", "by_layer")),
    "buffer": ("spatial_buffer", ("layer", "distance_m")),
    "centroid": ("spatial_centroid", ("layer",)),
    "dissolve": ("spatial_dissolve", ("layer", "by_columns?")),
    "convex_hull": ("spatial_convex_hull", ("layer", "aggregate?")),
    "intersect": ("spatial_intersect", ("a_layer", "b_layer")),
    "select_by_location": ("spatial_select_by_location",
                            ("layer", "by_layer", "predicate", "distance_m?")),
}
