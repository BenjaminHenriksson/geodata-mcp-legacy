"""Build data/normalized/osm/addresses.parquet from the Geofabrik Sweden extract.

Output schema (EPSG:3011):
    street         VARCHAR
    number         VARCHAR
    postcode       VARCHAR (optional, may be NULL)
    city           VARCHAR (optional, may be NULL)
    source         VARCHAR ('osm_node' | 'osm_way_centroid')
    osm_id         BIGINT
    x_3011         DOUBLE
    y_3011         DOUBLE
    geom           GEOMETRY(Point, EPSG:3011)

Two OSM sources collapse into the same row shape:

  - Addressed **nodes** — individual address points. Coords are the node lat/lon.
  - Addressed **ways** — building footprints with address tags. Coords are the
    unweighted centroid of the member nodes (more than accurate enough for
    building-level geocoding).

Stockholm kommun bbox (WGS84, generous): lon 17.78–18.25, lat 59.20–59.45.
The final parquet filters to Stockholm kommun plus a small margin.

Usage:
    .venv/bin/python scripts/fetch_osm.py            # read existing pbf + extract
    .venv/bin/python scripts/fetch_osm.py --download  # also refresh the pbf

No external runtime dependency — everything bakes into the parquet at
normalize time. The server only ever reads the parquet.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
PBF = ROOT / "data/osm/sweden-latest.osm.pbf"
OUT_DIR = ROOT / "data/normalized/osm"
OUT = OUT_DIR / "addresses.parquet"

PBF_URL = "https://download.geofabrik.de/europe/sweden-latest.osm.pbf"

# Stockholm kommun bbox in WGS84 — generous (covers the whole municipality
# plus ~1 km margin). Refined downstream via the normalized kommun polygon.
LON_MIN, LON_MAX = 17.78, 18.25
LAT_MIN, LAT_MAX = 59.20, 59.45


def ensure_pbf(download: bool) -> None:
    PBF.parent.mkdir(parents=True, exist_ok=True)
    if download or not PBF.exists():
        print(f"[osm] downloading {PBF_URL} → {PBF}")
        subprocess.check_call([
            "curl", "-sSL", "--progress-bar",
            "-o", str(PBF), PBF_URL,
        ])
        print(f"[osm] downloaded {PBF.stat().st_size/1024/1024:.0f} MB")


def extract() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[osm] extracting addressed features from {PBF.name}")

    # A dedicated temp dir that ben owns outright, so this script's run
    # identity doesn't have to carry the geodata-mcp supplementary group.
    # (The service's .duckdb/tmp is owned by the geodata-mcp group and
    # systemd-run --uid=ben doesn't inherit ben's supplementary groups.)
    tmp_dir = ROOT / "data" / "osm" / "_duckdb_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect()
    conn.execute("INSTALL spatial; LOAD spatial;")
    # Box has 4 GB total / 2 vCPU. Leave one core + ~1.7 GB for the rest of
    # the system (service, kernel, cache). Python/arrow overhead sits outside
    # the DuckDB limit, so keep DuckDB's own allowance below the cgroup
    # ceiling the caller enforces via systemd-run.
    conn.execute("SET memory_limit='1600MB'")
    conn.execute("SET threads=1")
    conn.execute("SET preserve_insertion_order=false")
    conn.execute(f"SET temp_directory = '{tmp_dir}'")

    # Pass 1: addressed nodes (directly usable — they carry lat/lon).
    print("  pass 1/2: addressed nodes...")
    conn.execute(f"""
        CREATE TEMP TABLE addr_nodes AS
        SELECT
            id AS osm_id,
            tags['addr:street'] AS street,
            tags['addr:housenumber'] AS number,
            tags['addr:postcode'] AS postcode,
            tags['addr:city'] AS city,
            lat, lon
        FROM ST_ReadOSM('{PBF}')
        WHERE kind = 'node'
          AND lon BETWEEN {LON_MIN} AND {LON_MAX}
          AND lat BETWEEN {LAT_MIN} AND {LAT_MAX}
          AND tags['addr:street'] IS NOT NULL
          AND tags['addr:housenumber'] IS NOT NULL
    """)
    n_nodes = conn.execute("SELECT COUNT(*) FROM addr_nodes").fetchone()[0]
    print(f"    {n_nodes:,} addressed nodes")

    # Pass 1 also gets addressed ways (no bbox filter yet — ways don't carry
    # coords). We'll narrow to Stockholm after resolving centroids.
    conn.execute(f"""
        CREATE TEMP TABLE addr_ways AS
        SELECT
            id AS osm_id,
            tags['addr:street'] AS street,
            tags['addr:housenumber'] AS number,
            tags['addr:postcode'] AS postcode,
            tags['addr:city'] AS city,
            refs
        FROM ST_ReadOSM('{PBF}')
        WHERE kind = 'way'
          AND tags['addr:street'] IS NOT NULL
          AND tags['addr:housenumber'] IS NOT NULL
    """)
    n_ways = conn.execute("SELECT COUNT(*) FROM addr_ways").fetchone()[0]
    print(f"    {n_ways:,} addressed ways (nationally)")

    # Build the unique node-id set we need for centroid resolution.
    conn.execute("""
        CREATE TEMP TABLE ref_ids AS
        SELECT DISTINCT UNNEST(refs) AS nid FROM addr_ways
    """)
    n_refs = conn.execute("SELECT COUNT(*) FROM ref_ids").fetchone()[0]
    print(f"    {n_refs:,} unique node refs to resolve")

    # Pass 2: resolve referenced nodes' coords.
    print("  pass 2/2: resolving way-node coords...")
    conn.execute(f"""
        CREATE TEMP TABLE ref_coords AS
        SELECT n.id, n.lat, n.lon
        FROM ST_ReadOSM('{PBF}') n
        JOIN ref_ids r ON n.id = r.nid
        WHERE n.kind = 'node'
    """)
    n_resolved = conn.execute("SELECT COUNT(*) FROM ref_coords").fetchone()[0]
    print(f"    {n_resolved:,} node coords resolved")

    # Centroid per way + Stockholm bbox filter.
    print("  computing way centroids + filtering to Stockholm bbox...")
    conn.execute(f"""
        CREATE TEMP TABLE way_centroids AS
        WITH unnested AS (
            SELECT w.osm_id, w.street, w.number, w.postcode, w.city,
                   UNNEST(w.refs) AS nid
            FROM addr_ways w
        ),
        joined AS (
            SELECT u.osm_id, u.street, u.number, u.postcode, u.city,
                   c.lat, c.lon
            FROM unnested u
            JOIN ref_coords c ON u.nid = c.id
        )
        SELECT osm_id, street, number, postcode, city,
               AVG(lat) AS lat, AVG(lon) AS lon
        FROM joined
        GROUP BY osm_id, street, number, postcode, city
        HAVING AVG(lon) BETWEEN {LON_MIN} AND {LON_MAX}
           AND AVG(lat) BETWEEN {LAT_MIN} AND {LAT_MAX}
    """)
    n_centroids = conn.execute("SELECT COUNT(*) FROM way_centroids").fetchone()[0]
    print(f"    {n_centroids:,} way centroids in Stockholm bbox")

    # Union + reproject to EPSG:3011.
    print("  reprojecting + writing parquet...")
    conn.execute(f"""
        COPY (
            SELECT
                TRIM(street) AS street,
                TRIM(number) AS number,
                postcode,
                city,
                source,
                osm_id,
                ST_X(pt_3011) AS x_3011,
                ST_Y(pt_3011) AS y_3011,
                pt_3011 AS geom
            FROM (
                SELECT street, number, postcode, city, 'osm_node' AS source, osm_id,
                       ST_Transform(ST_Point(lon, lat), 'EPSG:4326', 'EPSG:3011', true) AS pt_3011
                FROM addr_nodes
                UNION ALL
                SELECT street, number, postcode, city, 'osm_way_centroid' AS source, osm_id,
                       ST_Transform(ST_Point(lon, lat), 'EPSG:4326', 'EPSG:3011', true) AS pt_3011
                FROM way_centroids
            )
            WHERE street IS NOT NULL AND number IS NOT NULL
        )
        TO '{OUT}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    n_total = conn.execute(
        f"SELECT COUNT(*) FROM read_parquet('{OUT}')"
    ).fetchone()[0]
    print(f"  wrote {n_total:,} rows → {OUT.relative_to(ROOT)}  "
          f"({OUT.stat().st_size/1024/1024:.1f} MB)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true",
                        help="re-download the Geofabrik Sweden PBF")
    args = parser.parse_args()
    ensure_pbf(args.download)
    extract()


if __name__ == "__main__":
    main()
