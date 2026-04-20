"""Server-side map rendering. Produces an editorial PNG of one-or-more
session layers over a Carto Positron basemap, honouring the same style
spec (color/opacity/size/stroke channels) the viewer uses for `show()`.

Design principle: Claude can't fetch tiles, reproject them, or composite
a styled vector overlay, so we render it here and hand back a URL. We
don't build a PDF layout around it — Claude can assemble that itself
once it has the map image.

The basemap is read from a local tile cache under
`data/basemap/positron/`. The service runs with outbound egress denied,
so runtime fetches aren't an option; `scripts/fetch_basemap.py` pre-warms
the cache offline. If the cache is missing, the renderer falls back to a
paper-only backdrop (the old behaviour).
"""
from __future__ import annotations

import io
import json
import math
import secrets
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless; no X server inside systemd sandbox.
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from matplotlib.collections import PatchCollection, LineCollection
from matplotlib.patches import Polygon as MplPolygon
from PIL import Image
from pyproj import Transformer
from shapely.geometry import shape as shapely_shape

from .session import Session

_BASEMAP_DIR = (Path(__file__).resolve().parents[1]
                / "data" / "basemap" / "positron")
_TILE_SIZE = 256
# Zooms for which we have cached tiles — must match fetch_basemap.py.
_AVAILABLE_ZOOMS = (10, 11, 12, 13)

# Cached reprojection transformer. pyproj is thread-safe once constructed.
_T_3011_TO_4326 = Transformer.from_crs(3011, 4326, always_xy=True)
_T_4326_TO_3011 = Transformer.from_crs(4326, 3011, always_xy=True)

# --- palette --------------------------------------------------------------

IVORY = "#F5F0E8"
PAPER = "#EDE7D9"
INK = "#1C1A16"
INK_MID = "#4A4540"
INK_FAINT = "#9A9188"
TERRA = "#B05B3B"
RULE_SOFT = (0.11, 0.10, 0.09, 0.08)   # rgba-ish for light grid

DEFAULT_COLORS = [
    "#B05B3B", "#7A4A35", "#8C6A3E", "#5C5040", "#6B7348",
    "#4F5E6B", "#8A5A7A", "#4E7F7A", "#A5462F", "#9A7A42",
]


# --- feature fetch --------------------------------------------------------

def _fetch_features(sess: Session, layer: str) -> list[dict]:
    """Return a list of {geom, props} for every feature in the layer.
    Geometry is a shapely object in EPSG:3011; props is the raw dict."""
    meta = sess.layers.get(layer)
    if meta is None:
        raise ValueError(f"unknown layer '{layer}'")
    geom_col = meta.attributes.get("__geom_col__") or ""
    if not geom_col:
        return []
    cols = [c for c in meta.attributes
            if not c.startswith("__") and c != geom_col]
    props_struct = ", ".join(f"'{c}', \"{c}\"" for c in cols) or "'_', NULL"
    sql = f"""
        SELECT
            ST_AsGeoJSON("{geom_col}")::VARCHAR AS g,
            json_object({props_struct})::VARCHAR AS p
        FROM "{layer}"
    """
    out = []
    for g, p in sess.conn.execute(sql).fetchall():
        if not g:
            continue
        try:
            geom = shapely_shape(json.loads(g))
            props = json.loads(p) if p else {}
        except Exception:
            continue
        out.append({"geom": geom, "props": props})
    return out


# --- style channel helpers ------------------------------------------------

def _linear_map(val, src_lo, src_hi, dst_lo, dst_hi):
    if src_hi == src_lo:
        return (dst_lo + dst_hi) / 2
    t = (val - src_lo) / (src_hi - src_lo)
    t = max(0.0, min(1.0, t))
    return dst_lo + t * (dst_hi - dst_lo)


def _numeric_range(feats: list[dict], col: str):
    vals = []
    for f in feats:
        v = f["props"].get(col)
        if isinstance(v, (int, float)):
            vals.append(v)
    if not vals:
        return None
    return min(vals), max(vals)


def _resolve_color(spec: dict | None, default: str, feats: list[dict]):
    """Return either a constant color (str) or a callable f(feature) → str."""
    if not spec or not spec.get("column"):
        return lambda f: default
    col = spec["column"]
    scale = spec.get("scale") or "categorical"
    palette = spec.get("palette")

    if scale == "linear" and isinstance(palette, list) and len(palette) >= 2:
        rng = _numeric_range(feats, col)
        if rng is None:
            return lambda f: default
        lo, hi = rng
        lo_c = _hex_to_rgb(palette[0])
        hi_c = _hex_to_rgb(palette[-1])
        def color(f):
            v = f["props"].get(col)
            if not isinstance(v, (int, float)):
                return default
            t = (v - lo) / (hi - lo) if hi != lo else 0.5
            t = max(0.0, min(1.0, t))
            r = lo_c[0] + t * (hi_c[0] - lo_c[0])
            g = lo_c[1] + t * (hi_c[1] - lo_c[1])
            b = lo_c[2] + t * (hi_c[2] - lo_c[2])
            return (r, g, b)
        return color

    # categorical — auto-assign if no palette
    if not isinstance(palette, dict):
        distinct = []
        seen = set()
        for f in feats:
            v = f["props"].get(col)
            if v is None or v == "":
                continue
            k = str(v)
            if k in seen:
                continue
            seen.add(k)
            distinct.append(v)
        palette = {str(v): DEFAULT_COLORS[i % len(DEFAULT_COLORS)]
                   for i, v in enumerate(distinct)}
        spec["palette"] = palette  # mutate so legend reads the assignment
    lookup = dict(palette)
    def color(f):
        v = f["props"].get(col)
        return lookup.get(str(v), default)
    return color


def _resolve_channel(spec: dict | None, feats: list[dict], fallback: float):
    """For size/opacity/stroke linear-map channels."""
    if not spec or not spec.get("column"):
        return lambda f: fallback
    col = spec["column"]
    rng_out = spec.get("range")
    if not (isinstance(rng_out, list) and len(rng_out) == 2):
        return lambda f: fallback
    rng_in = _numeric_range(feats, col)
    if rng_in is None:
        return lambda f: fallback
    lo_in, hi_in = rng_in
    lo_out, hi_out = float(rng_out[0]), float(rng_out[1])
    def ch(f):
        v = f["props"].get(col)
        if not isinstance(v, (int, float)):
            return fallback
        return _linear_map(v, lo_in, hi_in, lo_out, hi_out)
    return ch


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return (int(h[0:2], 16)/255, int(h[2:4], 16)/255, int(h[4:6], 16)/255)


# --- drawing --------------------------------------------------------------

def _draw_layer(ax, feats, spec, default_color):
    """Draw every feature onto the axes with the given style spec."""
    color_fn = _resolve_color(spec, default_color, feats)
    opacity_fn = _resolve_channel(spec.get("opacity") if spec else None, feats, 1.0)
    size_fn = _resolve_channel(spec.get("size") if spec else None, feats, 18.0)
    stroke_fn = _resolve_channel(spec.get("stroke") if spec else None, feats, 1.0)

    pt_xs, pt_ys, pt_cs, pt_ss, pt_as = [], [], [], [], []
    for f in feats:
        g = f["geom"]
        c = color_fn(f)
        a = opacity_fn(f)
        sz = size_fn(f)
        sw = stroke_fn(f)

        t = g.geom_type
        if t == "Point":
            pt_xs.append(g.x); pt_ys.append(g.y)
            pt_cs.append(c); pt_ss.append(sz**2); pt_as.append(a)
        elif t == "MultiPoint":
            for p in g.geoms:
                pt_xs.append(p.x); pt_ys.append(p.y)
                pt_cs.append(c); pt_ss.append(sz**2); pt_as.append(a)
        elif t in ("LineString", "MultiLineString"):
            lines = [g] if t == "LineString" else list(g.geoms)
            segs = [list(ln.coords) for ln in lines]
            lc = LineCollection(segs, colors=[c]*len(segs),
                                linewidths=[max(0.5, sw)]*len(segs),
                                alpha=a, capstyle="round")
            ax.add_collection(lc)
        elif t in ("Polygon", "MultiPolygon"):
            polys = [g] if t == "Polygon" else list(g.geoms)
            for poly in polys:
                ext = list(poly.exterior.coords)
                patch = MplPolygon(ext, closed=True,
                                   facecolor=c, edgecolor=c,
                                   linewidth=max(0.3, sw * 0.6),
                                   alpha=min(1.0, a * 0.5),
                                   joinstyle="round")
                ax.add_patch(patch)
                # darker outline stroke
                outline = MplPolygon(ext, closed=True, fill=False,
                                     edgecolor=c, linewidth=max(0.4, sw),
                                     alpha=a, joinstyle="round")
                ax.add_patch(outline)
                for hole in poly.interiors:
                    hole_ext = list(hole.coords)
                    hp = MplPolygon(hole_ext, closed=True, fill=True,
                                    facecolor=IVORY, edgecolor=c,
                                    linewidth=max(0.3, sw * 0.4))
                    ax.add_patch(hp)
    if pt_xs:
        ax.scatter(pt_xs, pt_ys, c=pt_cs, s=pt_ss, alpha=None,
                   edgecolors=IVORY, linewidths=0.8,
                   zorder=5)


def _compute_extent(all_feats):
    xmin = ymin = float("inf")
    xmax = ymax = float("-inf")
    for f in all_feats:
        g = f["geom"]
        b = g.bounds  # (xmin, ymin, xmax, ymax)
        if b[0] < xmin: xmin = b[0]
        if b[1] < ymin: ymin = b[1]
        if b[2] > xmax: xmax = b[2]
        if b[3] > ymax: ymax = b[3]
    if xmin == float("inf"):
        # Stockholm kommun fallback (rough bbox in EPSG:3011)
        return 143000, 6565000, 165000, 6595000
    # 8% margin so features don't butt up against the edge
    dx = xmax - xmin
    dy = ymax - ymin
    pad = 0.08 * max(dx, dy, 100)
    return xmin - pad, ymin - pad, xmax + pad, ymax + pad


def _scale_bar(ax, x0, y0, dx):
    """Editorial scale bar. `dx` = horizontal data-range span of the
    plot so we can pick a sensible length."""
    # Pick a target length ~15-25% of the map width, rounded to 1-2-5
    target = dx * 0.18
    mag = 10 ** int(max(0, __import__('math').floor(__import__('math').log10(target))))
    for m in (1, 2, 5, 10):
        if mag * m >= target:
            length = mag * m
            break
    else:
        length = mag * 10
    # Draw a double-height bar
    ax.plot([x0, x0 + length], [y0, y0], color=INK, linewidth=1.6,
            solid_capstyle="butt", zorder=20)
    ax.plot([x0, x0], [y0 - length*0.015, y0 + length*0.015], color=INK,
            linewidth=1.4, zorder=20)
    ax.plot([x0 + length, x0 + length],
            [y0 - length*0.015, y0 + length*0.015],
            color=INK, linewidth=1.4, zorder=20)
    txt = f"{int(length)} m" if length < 1000 else f"{length/1000:g} km"
    ax.text(x0 + length / 2, y0 + length * 0.04, txt,
            fontsize=8, ha="center", va="bottom",
            color=INK, family="DejaVu Sans",
            zorder=20)


def _legend_entries(name, spec, default_color):
    """Build legend entries for a single layer's style."""
    if not spec or not spec.get("column"):
        return [(name, default_color, "patch")]
    col = spec["column"]
    scale = spec.get("scale") or "categorical"
    palette = spec.get("palette") or {}
    entries = []
    # sub-header
    entries.append((f"{name} · {col}", None, "header"))
    if scale == "linear" and isinstance(palette, list) and len(palette) >= 2:
        entries.append((f"{palette[0]} → {palette[-1]}", palette[-1], "gradient"))
    elif isinstance(palette, dict):
        for v, c in list(palette.items())[:10]:
            entries.append((str(v), c, "patch"))
        if len(palette) > 10:
            entries.append((f"+{len(palette)-10} more", None, "more"))
    return entries


# --- top-level entry ------------------------------------------------------

def render_map_png(
    sess: Session,
    layers: list[str],
    out_dir: Path,
    *,
    title: str | None = None,
    legend: bool = True,
    width_px: int = 1600,
    height_px: int = 1000,
    dpi: int = 150,
) -> dict:
    """Render the given layers to a PNG inside `out_dir`. Returns
    `{filename, width, height, bbox_3011}`."""
    if not layers:
        raise ValueError("render_map needs at least one layer")
    resolved = []
    for name in layers:
        if name not in sess.layers:
            raise ValueError(
                f"unknown layer '{name}'. Available: {list(sess.layers)}"
            )
        resolved.append(name)

    # Pull features + compute extent across all visible layers.
    per_layer = []
    all_feats = []
    for name in resolved:
        feats = _fetch_features(sess, name)
        per_layer.append((name, feats))
        all_feats.extend(feats)
    xmin, ymin, xmax, ymax = _compute_extent(all_feats)

    # Preserve data aspect ratio — otherwise the map distorts.
    data_aspect = (ymax - ymin) / max(1e-6, (xmax - xmin))
    fig_aspect = height_px / max(1, width_px)
    if data_aspect > fig_aspect:
        # data is taller than figure: expand x range
        needed_w = (ymax - ymin) / fig_aspect
        cx = (xmin + xmax) / 2
        xmin = cx - needed_w / 2
        xmax = cx + needed_w / 2
    else:
        # data is wider than figure: expand y range
        needed_h = (xmax - xmin) * fig_aspect
        cy = (ymin + ymax) / 2
        ymin = cy - needed_h / 2
        ymax = cy + needed_h / 2

    # Figure setup. Paper-toned background. Leave room on the right for
    # a legend column (~18% of width) and on the top for a title strip.
    fig = plt.figure(
        figsize=(width_px / dpi, height_px / dpi),
        dpi=dpi, facecolor=IVORY,
    )
    has_legend = legend and bool(per_layer)
    right_margin = 0.2 if has_legend else 0.04
    ax = fig.add_axes([0.04, 0.06, 1 - 0.04 - right_margin, 0.84])
    ax.set_facecolor(IVORY)
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")

    for spine in ax.spines.values():
        spine.set_visible(False)

    # Carto Positron basemap from the local tile cache. Falls back to a
    # faint cartographer grid on an ivory backdrop when the cache isn't
    # available (first run after clone, or extent outside kommun 0180).
    basemap_ok = _draw_basemap(ax, xmin, ymin, xmax, ymax)
    if not basemap_ok:
        step = _grid_step(xmax - xmin)
        gx = math.floor(xmin / step) * step
        while gx < xmax:
            ax.axvline(gx, color=INK, alpha=0.04, linewidth=0.5, zorder=0)
            gx += step
        gy = math.floor(ymin / step) * step
        while gy < ymax:
            ax.axhline(gy, color=INK, alpha=0.04, linewidth=0.5, zorder=0)
            gy += step

    # Draw layers in supplied order.
    styles = sess.visible_styles or {}
    for i, (name, feats) in enumerate(per_layer):
        default = DEFAULT_COLORS[i % len(DEFAULT_COLORS)]
        _draw_layer(ax, feats, styles.get(name), default)

    # Eyebrow label (cartographer's annotation), then title below.
    fig.text(0.04, 0.955, "GEODATA MCP · STOCKHOLM · SWEREF 99 18 00",
             fontsize=8, color=TERRA, ha="left", va="top",
             family="sans-serif")
    if title or sess.visible_title:
        t = title or sess.visible_title
        fig.text(0.04, 0.935, t, fontsize=16, color=INK,
                 family="serif", ha="left", va="top", weight="normal")

    # Scale bar, bottom-left
    _scale_bar(ax, xmin + (xmax - xmin) * 0.04,
               ymin + (ymax - ymin) * 0.06, xmax - xmin)

    # Basemap attribution, per Carto + OSM licence terms.
    if basemap_ok:
        fig.text(
            0.99, 0.02,
            "© OpenStreetMap contributors · © CARTO",
            fontsize=7, color=INK_FAINT,
            family="sans-serif", ha="right", va="bottom",
        )

    # Legend, right-hand column
    if legend:
        _draw_legend(fig, per_layer, styles)

    # Output
    filename = f"map_{secrets.token_urlsafe(8)}.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    fig.savefig(out_path, facecolor=IVORY, dpi=dpi,
                pil_kwargs={"optimize": True})
    plt.close(fig)
    return {
        "filename": filename,
        "width": width_px,
        "height": height_px,
        "bbox_3011": [xmin, ymin, xmax, ymax],
    }


# --- basemap tile compositing -------------------------------------------

def _deg2tile(lat_deg: float, lon_deg: float, zoom: int) -> tuple[int, int]:
    lat_rad = math.radians(lat_deg)
    n = 2 ** zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def _tile2deg(x: int, y: int, zoom: int) -> tuple[float, float]:
    n = 2 ** zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


def _pick_zoom(lon_span_deg: float) -> int:
    """Pick the smallest cached zoom such that the extent spans at
    least ~4 tiles horizontally (so detail is visible) but not more
    than a few dozen (to cap compositing cost)."""
    for z in _AVAILABLE_ZOOMS:
        tiles_across = (lon_span_deg / 360.0) * (2 ** z)
        if tiles_across >= 2.0:
            # Prefer the next zoom up if we're still well under ~10 tiles.
            if tiles_across < 5 and z + 1 in _AVAILABLE_ZOOMS:
                return z + 1
            return z
    return _AVAILABLE_ZOOMS[-1]


def _draw_basemap(ax, xmin, ymin, xmax, ymax) -> bool:
    """Composite cached Carto Positron tiles as the map background.
    Returns True on success, False when the cache is missing or doesn't
    cover the requested extent (caller falls back to paper backdrop)."""
    if not _BASEMAP_DIR.exists():
        return False

    # Convert the 3011 extent to WGS84 to pick tiles.
    lon_min, lat_min = _T_3011_TO_4326.transform(xmin, ymin)
    lon_max, lat_max = _T_3011_TO_4326.transform(xmax, ymax)
    if not (lon_min < lon_max and lat_min < lat_max):
        return False

    zoom = _pick_zoom(lon_max - lon_min)
    x0, y0 = _deg2tile(lat_max, lon_min, zoom)   # NW
    x1, y1 = _deg2tile(lat_min, lon_max, zoom)   # SE
    if x1 < x0 or y1 < y0:
        return False

    cols = x1 - x0 + 1
    rows = y1 - y0 + 1
    # Guardrail: don't composite more than ~64 tiles for one render.
    if cols * rows > 64:
        return False

    big = Image.new("RGB", (cols * _TILE_SIZE, rows * _TILE_SIZE),
                    (245, 240, 232))
    any_tile = False
    for ix, x in enumerate(range(x0, x1 + 1)):
        for iy, y in enumerate(range(y0, y1 + 1)):
            path = _BASEMAP_DIR / str(zoom) / str(x) / f"{y}.png"
            if not path.exists():
                continue
            try:
                tile = Image.open(path).convert("RGB")
                big.paste(tile, (ix * _TILE_SIZE, iy * _TILE_SIZE))
                any_tile = True
            except Exception:
                continue
    if not any_tile:
        return False

    # Composite bounds in WGS84 come from the tile edges.
    nw_lat, nw_lon = _tile2deg(x0, y0, zoom)
    se_lat, se_lon = _tile2deg(x1 + 1, y1 + 1, zoom)
    # Reproject the composite's four corners to 3011 and use as imshow
    # extent. Between 3011 and the Mercator tile projection there's a
    # small sub-percent distortion over the kommun; visually acceptable
    # for an editorial artefact.
    px_min, py_max = _T_4326_TO_3011.transform(nw_lon, nw_lat)
    px_max, py_min = _T_4326_TO_3011.transform(se_lon, se_lat)
    ax.imshow(
        big,
        extent=(px_min, px_max, py_min, py_max),
        origin="upper",
        zorder=0,
        alpha=0.9,  # mute slightly so vector overlay reads cleanly
        interpolation="bilinear",
    )
    return True


def _grid_step(dx):
    """Pick a grid step: 100/200/500/1000/... that gives 5-12 lines."""
    import math as _m
    target = dx / 8
    mag = 10 ** int(_m.floor(_m.log10(max(1, target))))
    for m in (1, 2, 5, 10):
        if mag * m >= target:
            return mag * m
    return mag * 10


def _draw_legend(fig, per_layer, styles):
    """Right-side legend stacking per-layer entries."""
    lines = []
    for i, (name, _feats) in enumerate(per_layer):
        default = DEFAULT_COLORS[i % len(DEFAULT_COLORS)]
        lines.extend(_legend_entries(name, styles.get(name), default))
    if not lines:
        return
    n = min(32, len(lines))
    # Legend column: right margin is reserved via the axes layout above.
    x_sw = 0.815              # swatch left edge
    x_txt = 0.835             # text left edge (18% right-margin - swatch)
    y_top = 0.88
    dy = 0.024
    MAX_LABEL = 22            # truncate longer labels with ellipsis
    def _trunc(s):
        return s if len(s) <= MAX_LABEL else s[: MAX_LABEL - 1] + "…"
    for i, (label, color, kind) in enumerate(lines[:n]):
        y = y_top - i * dy
        if kind == "header":
            fig.text(x_sw, y, _trunc(label), fontsize=9, color=TERRA,
                     family="sans-serif", weight="normal",
                     ha="left", va="top")
        elif kind == "gradient":
            fig.patches.append(Rectangle(
                (x_sw, y - 0.010), 0.015, 0.014,
                facecolor=color, edgecolor=INK,
                linewidth=0.4, transform=fig.transFigure, figure=fig,
            ))
            fig.text(x_txt, y, _trunc(label), fontsize=8.5, color=INK,
                     family="sans-serif", ha="left", va="top")
        elif kind == "patch":
            fig.patches.append(Rectangle(
                (x_sw, y - 0.010), 0.015, 0.014,
                facecolor=color if color else INK_FAINT,
                edgecolor=INK, linewidth=0.4,
                transform=fig.transFigure, figure=fig,
            ))
            fig.text(x_txt, y, _trunc(label), fontsize=8.5, color=INK,
                     family="sans-serif", ha="left", va="top")
        elif kind == "more":
            fig.text(x_txt, y, label, fontsize=8, color=INK_FAINT,
                     family="sans-serif", style="italic",
                     ha="left", va="top")
