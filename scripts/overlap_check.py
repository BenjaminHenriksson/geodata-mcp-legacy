"""Compare SBK Stockholm kommun polygon with the union of DeSO polygons
whose kommunkod = 0180. Both compared in EPSG:3011 (SBK native)."""
from __future__ import annotations
from pathlib import Path
from osgeo import ogr, osr

ogr.UseExceptions()

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SBK_ADM = DATA / "stockholm_sbk/Stadskarta_hela_Stockholm/Stockholm_Utskr-Stadskarta-Standard_shp/Adm_area.shp"
DESO_2025 = DATA / "scb_deso/geographic/DeSO_2025.gpkg"
DESO_2018 = DATA / "scb_deso/geographic/DeSO_2018.gpkg"

EPSG_WORK = 3011  # SBK local — working CRS


def sbk_kommun() -> ogr.Geometry:
    ds = ogr.Open(str(SBK_ADM))
    layer = ds.GetLayer(0)
    layer.SetAttributeFilter("KATEGORI = 'Kommun'")
    feats = list(layer)
    return feats[0].GetGeometryRef().Clone()


def deso_union(path: Path) -> ogr.Geometry:
    ds = ogr.Open(str(path))
    layer = ds.GetLayer(0)
    layer.SetAttributeFilter("kommunkod = '0180'")
    src_srs = layer.GetSpatialRef().Clone()
    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst_srs = osr.SpatialReference(); dst_srs.ImportFromEPSG(EPSG_WORK)
    dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    xform = osr.CoordinateTransformation(src_srs, dst_srs)

    geoms = []
    for feat in layer:
        g = feat.GetGeometryRef().Clone()
        g.Transform(xform)
        geoms.append(g)

    # Cascaded union
    coll = ogr.Geometry(ogr.wkbMultiPolygon)
    for g in geoms:
        if g.GetGeometryType() in (6, 1006):
            for i in range(g.GetGeometryCount()):
                coll.AddGeometry(g.GetGeometryRef(i).Clone())
        else:
            coll.AddGeometry(g)
    return coll.UnionCascaded()


def main() -> None:
    sbk = sbk_kommun()
    sbk_area = sbk.GetArea() / 1e6
    sbk_env = sbk.GetEnvelope()

    print(f"SBK Stockholm kommun: area = {sbk_area:.2f} km²")
    print(f"  bbox (E,N): E {sbk_env[0]:.0f}-{sbk_env[1]:.0f}  "
          f"N {sbk_env[2]:.0f}-{sbk_env[3]:.0f}\n")

    for deso_path, label in [(DESO_2025, "DeSO 2025"), (DESO_2018, "DeSO 2018")]:
        du = deso_union(deso_path)
        du_area = du.GetArea() / 1e6

        inter_area = sbk.Intersection(du).GetArea() / 1e6
        only_sbk = sbk.Difference(du).GetArea() / 1e6
        only_deso = du.Difference(sbk).GetArea() / 1e6
        union_area = sbk.Union(du).GetArea() / 1e6
        iou = inter_area / union_area if union_area else 0
        sym_diff = only_sbk + only_deso

        print(f"=== {label} kommunkod=0180 union ===")
        print(f"  area              = {du_area:>8.2f} km²  (SBK reports {sbk_area:.2f})")
        print(f"  intersection      = {inter_area:>8.2f} km²")
        print(f"  only in SBK       = {only_sbk:>8.2f} km²  ({100*only_sbk/sbk_area:.2f}% of SBK)")
        print(f"  only in DeSO      = {only_deso:>8.2f} km²  ({100*only_deso/du_area:.2f}% of DeSO)")
        print(f"  symmetric diff    = {sym_diff:>8.2f} km²")
        print(f"  IoU               = {iou:>8.4f}")
        print()


if __name__ == "__main__":
    main()
