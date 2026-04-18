"""Classify every DeSO polygon by its spatial relation to the Stockholm
municipality boundary from the SBK city map.

- Stockholm municipality = Adm_area.shp feature with KATEGORI='Kommun'
  (CRS EPSG:3011, reprojected here to EPSG:3006 SWEREF99 TM to match DeSO).
- DeSO polygons come from data/scb_deso/geographic/DeSO_2025.gpkg and
  DeSO_2018.gpkg (CRS EPSG:3006).

Every DeSO code starts with its municipality code (Stockholm = 0180), so the
results can be cross-checked against the code prefix.
"""
from __future__ import annotations

from pathlib import Path

from osgeo import ogr, osr

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

SBK_ADM = DATA / "stockholm_sbk/Stadskarta_hela_Stockholm/Stockholm_Utskr-Stadskarta-Standard_shp/Adm_area.shp"
DESO_2025 = DATA / "scb_deso/geographic/DeSO_2025.gpkg"
DESO_2018 = DATA / "scb_deso/geographic/DeSO_2018.gpkg"

SWEREF99_TM_EPSG = 3006
STOCKHOLM_KOMMUN_CODE = "0180"


def stockholm_polygon_in_sweref99tm() -> ogr.Geometry:
    ds = ogr.Open(str(SBK_ADM))
    layer = ds.GetLayer(0)
    layer.SetAttributeFilter("KATEGORI = 'Kommun'")
    feats = list(layer)
    assert len(feats) == 1, f"expected exactly 1 Kommun polygon, found {len(feats)}"
    geom = feats[0].GetGeometryRef().Clone()

    src_srs = layer.GetSpatialRef().Clone()
    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst_srs = osr.SpatialReference()
    dst_srs.ImportFromEPSG(SWEREF99_TM_EPSG)
    dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(src_srs, dst_srs)
    geom.Transform(transform)
    return geom


def classify(deso_path: Path, kommun_geom: ogr.Geometry) -> dict:
    ds = ogr.Open(str(deso_path))
    layer = ds.GetLayer(0)
    fields = {f.name for f in layer.schema}
    id_field = "desokod" if "desokod" in fields else "deso"
    kommun_field = "kommunkod" if "kommunkod" in fields else None

    counts = {"within": 0, "intersects_partial": 0, "disjoint": 0}
    code_match = {
        "within_and_kommun_0180": 0,
        "within_but_kommun_not_0180": 0,
        "kommun_0180_but_not_within": 0,
        "kommun_0180_total": 0,
    }
    partials: list[tuple[str, float, str]] = []

    total = layer.GetFeatureCount()
    kommun_env = kommun_geom.GetEnvelope()

    for feat in layer:
        code = feat.GetField(id_field) or ""
        kcode = feat.GetField(kommun_field) if kommun_field else code[:4]
        is_stockholm_code = kcode == STOCKHOLM_KOMMUN_CODE
        if is_stockholm_code:
            code_match["kommun_0180_total"] += 1
        geom = feat.GetGeometryRef()
        env = geom.GetEnvelope()
        if env[1] < kommun_env[0] or env[0] > kommun_env[1] or env[3] < kommun_env[2] or env[2] > kommun_env[3]:
            counts["disjoint"] += 1
            if is_stockholm_code:
                code_match["kommun_0180_but_not_within"] += 1
            continue
        if geom.Within(kommun_geom):
            counts["within"] += 1
            if is_stockholm_code:
                code_match["within_and_kommun_0180"] += 1
            else:
                code_match["within_but_kommun_not_0180"] += 1
        elif geom.Intersects(kommun_geom):
            inter = geom.Intersection(kommun_geom)
            frac = inter.GetArea() / geom.GetArea() if geom.GetArea() else 0
            counts["intersects_partial"] += 1
            partials.append((code, frac, kcode))
            if is_stockholm_code:
                code_match["kommun_0180_but_not_within"] += 1
        else:
            counts["disjoint"] += 1
            if is_stockholm_code:
                code_match["kommun_0180_but_not_within"] += 1

    return {
        "path": str(deso_path.relative_to(DATA)),
        "id_field": id_field,
        "kommun_field": kommun_field,
        "total": total,
        "counts": counts,
        "percentages": {k: 100.0 * v / total for k, v in counts.items()},
        "code_match": code_match,
        "partial_overlap_samples": sorted(partials, key=lambda x: x[1], reverse=True)[:10],
        "partial_overlap_count": len(partials),
    }


def main() -> None:
    kommun = stockholm_polygon_in_sweref99tm()
    print(f"Stockholm kommun polygon: area = {kommun.GetArea() / 1e6:.2f} km²")
    minx, maxx, miny, maxy = kommun.GetEnvelope()
    print(f"  envelope (SWEREF99 TM): E {minx:.0f}-{maxx:.0f}  N {miny:.0f}-{maxy:.0f}")

    for path in [DESO_2025, DESO_2018]:
        result = classify(path, kommun)
        print("\n=== " + result["path"] + " ===")
        print(f"  fields used: id={result['id_field']}  kommun={result['kommun_field']}")
        print(f"  total DeSO polygons: {result['total']}")
        for k in ["within", "intersects_partial", "disjoint"]:
            print(f"  {k:<20} {result['counts'][k]:>6}  ({result['percentages'][k]:.2f}%)")
        print("  Cross-check vs DeSO kommunkod=0180 (Stockholm):")
        for k, v in result["code_match"].items():
            print(f"  {k:<34} {v}")
        if result["partial_overlap_count"]:
            print(f"  Partial-overlap DeSO ({result['partial_overlap_count']} polygons); "
                  "top fraction inside Stockholm:")
            for code, frac, kcode in result["partial_overlap_samples"]:
                print(f"    {code} (kommun {kcode})  {frac*100:.3f}% of area inside")


if __name__ == "__main__":
    main()
