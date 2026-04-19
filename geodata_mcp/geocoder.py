"""Phase-2 geocoder.

Three capabilities:

1. **Place-name geocoding** against `NamnText_point` (Stadsdel, Distrikt,
   Kvarter, Gatunamn, Samhällsfunktionsbyggnad, …) via DuckDB
   `jaro_winkler_similarity`.

2. **Polygon-aware bbox** for admin-like matches: when a match is a Stadsdel /
   Distrikt / Stadsdelsnämndsområde / Kvarter and a polygon of that
   KATEGORI + NAMN exists in `Adm_area`, the returned bbox is the polygon's
   extent instead of a 200 m radius around the label point.

3. **Composite street + house-number geocoding**: if the query looks like
   `"<street name> <house number>"`, spatially pair the best Gatunamn label
   with the nearest AdressText (house-number) point, returning the address
   coordinate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
NAMN_GPKG = ROOT / "data/normalized/sbk/NamnText_point.gpkg"
ADR_GPKG = ROOT / "data/normalized/sbk/AdressText_point.gpkg"
ADM_GPKG = ROOT / "data/normalized/sbk/Adm_area.gpkg"
# OSM-derived structured addresses (street + number → coords). Built at
# normalize time from the Geofabrik Sweden extract; see scripts/fetch_osm.py.
# Used as the primary source for composite street+number queries because it's
# authoritative (the SBK cartographic labels are positioned for rendering,
# not as an address registry, and have gaps).
OSM_ADDR_PARQUET = ROOT / "data/normalized/osm/addresses.parquet"

GEOCODABLE_GROUPS = (
    "Stadsdel",
    "Stadsdelsnämndsområde",
    "Distrikt",
    "Kvarter",
    "Gatunamn",
    "Samhällsfunktionsbyggnad",
    "Bostadsbyggnad",
    "Verksamhetsbyggnad",
    "Idrottsanläggning",
    "Koloniområde",
    "Sjö",
    "Vattendrag",
    "Natur",
    "Trafikplats",
    "Bytesplats",
    "Markanläggning",
    "Övrig anläggning",
)

# Kinds whose bbox should come from the containing admin polygon.
POLYGON_BACKED_GROUPS = {
    "Stadsdel",
    "Stadsdelsnämndsområde",
    "Distrikt",
    "Kvarter",
}


@dataclass
class GeocodeMatch:
    name: str
    grupp: str
    kategori: str | None
    x_3011: float
    y_3011: float
    score: float
    bbox_3011: tuple[float, float, float, float]
    kind: str = "place"   # 'place' or 'address'


# A street + number pattern: letters/åäö/spaces, then a number, optional letter suffix.
_ADDRESS_RE = re.compile(
    r"^\s*(?P<street>[\wÅÄÖåäö\.\-\s]+?)\s+(?P<number>\d{1,4}[A-Za-z]?)\s*$"
)


def _polygon_bbox(conn: duckdb.DuckDBPyConnection, kategori: str, namn: str
                   ) -> tuple[float, float, float, float] | None:
    row = conn.execute(
        f"""
        SELECT ST_XMin(env), ST_YMin(env), ST_XMax(env), ST_YMax(env) FROM (
            SELECT ST_Extent(ST_Union_Agg(geom)) AS env
            FROM ST_Read(?) WHERE KATEGORI = ? AND UPPER(NAMN) = UPPER(?)
        )
        """,
        [str(ADM_GPKG), kategori, namn],
    ).fetchone()
    if row and all(v is not None for v in row):
        return tuple(float(x) for x in row)  # type: ignore
    return None


def _place_matches(
    conn: duckdb.DuckDBPyConnection, query: str, *,
    limit: int, bbox_radius_m: float,
) -> list[GeocodeMatch]:
    grupp_list = ", ".join(f"'{g}'" for g in GEOCODABLE_GROUPS)
    qlower = query.strip().lower()
    rows = conn.execute(
        f"""
        WITH candidates AS (
            SELECT
                COALESCE(NULLIF(NAMN, ''), TEXT) AS name,
                GRUPP, KATEGORI,
                ST_X(geom) AS x, ST_Y(geom) AS y,
                lower(COALESCE(NULLIF(NAMN, ''), TEXT)) AS name_lower
            FROM ST_Read(?)
            WHERE GRUPP IN ({grupp_list})
              AND COALESCE(NULLIF(NAMN, ''), TEXT) IS NOT NULL
        )
        SELECT name, GRUPP, KATEGORI, x, y,
               jaro_winkler_similarity(name_lower, ?) AS score
        FROM candidates
        QUALIFY ROW_NUMBER() OVER (PARTITION BY lower(name), GRUPP ORDER BY score DESC) = 1
        ORDER BY score DESC
        LIMIT ?
        """,
        [str(NAMN_GPKG), qlower, limit],
    ).fetchall()

    matches: list[GeocodeMatch] = []
    for name, grupp, kategori, x, y, score in rows:
        if grupp in POLYGON_BACKED_GROUPS:
            bbox = _polygon_bbox(conn, grupp, name)
            if bbox is None:
                bbox = (x - bbox_radius_m, y - bbox_radius_m,
                        x + bbox_radius_m, y + bbox_radius_m)
        else:
            bbox = (x - bbox_radius_m, y - bbox_radius_m,
                    x + bbox_radius_m, y + bbox_radius_m)
        matches.append(GeocodeMatch(
            name=name, grupp=grupp, kategori=kategori,
            x_3011=float(x), y_3011=float(y),
            score=float(score), bbox_3011=bbox,
            kind="place",
        ))
    return matches


def _address_matches_osm(
    conn: duckdb.DuckDBPyConnection, street: str, number: str, *,
    limit: int,
) -> list[GeocodeMatch]:
    """Primary composite-address path — OSM structured `(street, number)`
    lookup. Fuzzy-matches the street (case-insensitive), exact-matches the
    number. Returns empty list if no hit; caller falls back to the SBK
    spatial-pairing path."""
    if not OSM_ADDR_PARQUET.exists():
        return []
    # Threshold tuned to prefer returning nothing over returning the wrong
    # street. Exact (case-insensitive) matches score 1.0; a typo like
    # "Drotningatan" vs "Drottninggatan" scores ~0.88 and gets rejected
    # (user re-types). Cross-street confusion "Drottninggatan" vs
    # "Drottningholmsvägen" scores ~0.87 — rejected for the same reason.
    # Legitimate case/space variants ("Elin Falks Gata" vs "Elin Falks
    # gata") go through lowercase and score 1.0.
    rows = conn.execute(
        """
        WITH cand AS (
            SELECT street, number, source, x_3011, y_3011,
                   jaro_winkler_similarity(lower(street), lower(?)) AS sscore,
                   CASE WHEN lower(street) = lower(?) THEN 1 ELSE 0 END AS exact_match
            FROM read_parquet(?)
            WHERE number = ?
        )
        SELECT street, number, source, x_3011, y_3011, sscore
        FROM cand
        WHERE sscore > 0.92 OR exact_match = 1
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY lower(street)
            ORDER BY exact_match DESC, sscore DESC, source ASC
        ) = 1
        ORDER BY exact_match DESC, sscore DESC
        LIMIT ?
        """,
        [street, street, str(OSM_ADDR_PARQUET), number, limit],
    ).fetchall()

    matches = []
    for street_name, num, source, nx, ny, sscore in rows:
        matches.append(GeocodeMatch(
            name=f"{street_name} {num}",
            grupp="Adress",
            kategori=f"osm {source} street_sim={sscore:.3f}",
            x_3011=float(nx), y_3011=float(ny),
            # Strong score: the OSM match is authoritative, not distance-penalized.
            score=float(sscore),
            bbox_3011=(float(nx)-30, float(ny)-30, float(nx)+30, float(ny)+30),
            kind="address",
        ))
    return matches


def _address_matches_sbk(
    conn: duckdb.DuckDBPyConnection, street: str, number: str, *,
    limit: int, search_radius_m: float = 250.0,
) -> list[GeocodeMatch]:
    """Fallback: SBK spatial-pairing. Pair the best street-label points for
    `street` with the nearest AdressText points whose NAMN equals `number`
    within `search_radius_m`. Fragile because SBK is a cartographic dataset
    — kept as a backup for addresses OSM doesn't cover."""
    rows = conn.execute(
        f"""
        WITH street_pts AS (
            SELECT COALESCE(NULLIF(NAMN,''), TEXT) AS street,
                   ST_X(geom) AS sx, ST_Y(geom) AS sy, geom AS sgeom,
                   jaro_winkler_similarity(
                      lower(COALESCE(NULLIF(NAMN,''), TEXT)), lower(?)
                   ) AS sscore
            FROM ST_Read(?)
            WHERE GRUPP = 'Gatunamn'
              AND COALESCE(NULLIF(NAMN,''), TEXT) IS NOT NULL
        ),
        number_pts AS (
            SELECT TEXT AS num, geom AS ngeom, ST_X(geom) AS nx, ST_Y(geom) AS ny
            FROM ST_Read(?)
            WHERE GRUPP = 'Adressplats' AND TEXT = ?
        ),
        paired AS (
            SELECT s.street, s.sscore, n.num, n.nx, n.ny,
                   ST_Distance(s.sgeom, n.ngeom) AS dist
            FROM street_pts s
            JOIN number_pts n
              ON ST_Distance(s.sgeom, n.ngeom) < ?
            WHERE s.sscore > 0.7
        )
        SELECT street, num, nx, ny, dist, sscore FROM paired
        QUALIFY ROW_NUMBER() OVER (PARTITION BY street, num ORDER BY dist ASC) = 1
        ORDER BY sscore DESC, dist ASC
        LIMIT ?
        """,
        [street, str(NAMN_GPKG), str(ADR_GPKG), number, float(search_radius_m), limit],
    ).fetchall()

    matches = []
    for street_name, num, nx, ny, dist, sscore in rows:
        combined = float(sscore) * max(0.0, 1.0 - (float(dist) / search_radius_m))
        matches.append(GeocodeMatch(
            name=f"{street_name} {num}",
            grupp="Adress",
            kategori=f"sbk street_sim={sscore:.3f} dist_m={dist:.1f} (fallback)",
            x_3011=float(nx), y_3011=float(ny),
            score=combined,
            bbox_3011=(float(nx)-30, float(ny)-30, float(nx)+30, float(ny)+30),
            kind="address",
        ))
    return matches


def _address_matches(
    conn: duckdb.DuckDBPyConnection, street: str, number: str, *,
    limit: int,
) -> list[GeocodeMatch]:
    """Composite street-number geocoder. Prefers OSM structured addresses;
    falls back to SBK spatial-pairing if OSM has no hit."""
    hits = _address_matches_osm(conn, street, number, limit=limit)
    if hits:
        return hits
    return _address_matches_sbk(conn, street, number, limit=limit)


def geocode(
    conn: duckdb.DuckDBPyConnection,
    query: str,
    *,
    limit: int = 5,
    bbox_radius_m: float = 200.0,
    all_kinds: bool = False,
) -> list[GeocodeMatch]:
    """Geocode a place name or `street number` against SBK label layers.

    When the input looks like `<street> <number>`, composite address matches
    (which may have lower raw similarity scores due to the distance penalty)
    are surfaced ABOVE place-name matches so the caller gets what they asked for.

    By default, place-name hits are collapsed across GRUPP — a building that
    appears in both `Samhällsfunktionsbyggnad` and `Kvarter enl detaljplan`
    only returns once (the highest-scored variant). Pass `all_kinds=True` to
    see every variant.
    """
    query = query.strip()
    addr_hits: list[GeocodeMatch] = []
    m = _ADDRESS_RE.match(query)
    if m:
        addr_hits = _address_matches(
            conn, m.group("street").strip(), m.group("number").strip(),
            limit=limit,
        )

    place_hits = _place_matches(
        conn, query, limit=limit, bbox_radius_m=bbox_radius_m,
    )

    def dedupe(items, collapse_grupp: bool):
        seen: dict[tuple, GeocodeMatch] = {}
        for r in items:
            # Either key on name alone (default: one hit per named thing) or
            # on (name, grupp) so Stadsdel-VASASTADEN and Kvarter-VASASTADEN
            # both show when the caller asked for all variants.
            key = (r.kind, r.name.lower()) if collapse_grupp else (r.kind, r.name.lower(), r.grupp)
            if key not in seen or r.score > seen[key].score:
                seen[key] = r
        return sorted(seen.values(), key=lambda r: r.score, reverse=True)

    # Addresses first when present, then place names.
    ranked = dedupe(addr_hits, collapse_grupp=not all_kinds) \
           + dedupe(place_hits, collapse_grupp=not all_kinds)
    return ranked[:limit]
