"""Pre-fetch Carto Positron tiles covering Stockholm kommun.

The MCP service runs with outbound egress denied, so `render_map` can't
fetch tiles at runtime. Instead we fetch once offline and stash tiles
under `data/basemap/positron/{z}/{x}/{y}.png`. The render.py code reads
from that cache and composites them as the map background.

Usage (run outside the sandbox, as a user with write access to the
repo):

    uv run python scripts/fetch_basemap.py

Rerun only to extend coverage or refresh the tile set. Existing tiles
are not re-downloaded.

Attribution: Carto Positron basemap, OpenStreetMap data. The tiles are
free to cache per Carto's attribution policy; the rendered PNG carries
the required credit line.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
import urllib.request

# Stockholm kommun bbox, slightly padded so renders framed to the edge
# still have tiles to draw. WGS84 lat/lon.
LAT_MIN = 59.20
LAT_MAX = 59.49
LON_MIN = 17.76
LON_MAX = 18.25

# Zoom range. z=10 gives city-level overview (2-4 tiles total); z=13
# gives neighbourhood detail. Beyond z=13 is rarely useful for the
# kinds of maps render_map produces and would blow up the tile count.
ZOOM_LEVELS = [10, 11, 12, 13]

OUT_DIR = (Path(__file__).resolve().parent.parent
           / "data" / "basemap" / "positron")

TILE_SERVERS = [
    "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    "https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    "https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    "https://d.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
]


def deg2tile(lat_deg: float, lon_deg: float, zoom: int) -> tuple[int, int]:
    """Slippy map tile index for a (lat, lon) at the given zoom level."""
    lat_rad = math.radians(lat_deg)
    n = 2 ** zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def fetch_tile(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "geodata-mcp basemap-fetcher (+https://geo.benjaminhenriksson.com/)"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def main() -> int:
    total = 0
    skipped = 0
    for z in ZOOM_LEVELS:
        # x increases east, y increases south (tile origin is top-left).
        x0, y0 = deg2tile(LAT_MAX, LON_MIN, z)   # NW corner
        x1, y1 = deg2tile(LAT_MIN, LON_MAX, z)   # SE corner
        count_z = (x1 - x0 + 1) * (y1 - y0 + 1)
        print(f"zoom {z}: {count_z} tiles "
              f"(x {x0}..{x1}, y {y0}..{y1})")
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                out = OUT_DIR / str(z) / str(x) / f"{y}.png"
                if out.exists():
                    skipped += 1
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                url = TILE_SERVERS[(x + y) % len(TILE_SERVERS)].format(
                    z=z, x=x, y=y,
                )
                try:
                    data = fetch_tile(url)
                except Exception as e:
                    print(f"  FAIL z={z} x={x} y={y}: {e}")
                    continue
                out.write_bytes(data)
                total += 1
                # Be polite to Carto's CDN.
                time.sleep(0.04)
                if total % 40 == 0:
                    print(f"  fetched {total} new tiles "
                          f"(latest: z={z} x={x} y={y})")
    print(f"done. fetched {total} new tiles, "
          f"skipped {skipped} already-cached tiles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
