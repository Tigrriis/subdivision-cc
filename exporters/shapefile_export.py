"""Zipped ESRI shapefile export (pyshp — pure python, PythonAnywhere friendly).

Output CRS: GDA94 / MGA zone 55 (EPSG:28355).
"""
import io
import zipfile

import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform

_TO_MGA = Transformer.from_crs(4326, 28355, always_xy=True)
_PRJ_WKT = CRS.from_epsg(28355).to_wkt("WKT1_ESRI")


def _to_mga_coords(geom_wgs):
    return mapping(transform(_TO_MGA.transform, shape(geom_wgs)))


def _poly_parts(geojson_geom):
    """GeoJSON polygon/multipolygon -> list of rings for pyshp."""
    g = geojson_geom
    rings = []
    polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    for poly in polys:
        for i, ring in enumerate(poly):
            pts = [(x, y) for x, y, *_ in ring]
            # pyshp: exterior rings clockwise, holes counter-clockwise
            area2 = sum((pts[j + 1][0] - pts[j][0]) * (pts[j + 1][1] + pts[j][1]) for j in range(len(pts) - 1))
            cw = area2 > 0
            if (i == 0 and not cw) or (i > 0 and cw):
                pts = pts[::-1]
            rings.append(pts)
    return rings


def _line_parts(geojson_geom):
    g = geojson_geom
    ls = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]
    return [[(x, y) for x, y, *_ in line] for line in ls]


def _write_layer(zf, name, fc, shp_type, fields):
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shp_type)
    for fname, ftype, size, dec in fields:
        w.field(fname, ftype, size, dec)
    for f in fc.get("features", []):
        gm = _to_mga_coords(f["geometry"])
        if shp_type == shapefile.POLYGON:
            w.poly(_poly_parts(gm))
        else:
            w.line(_line_parts(gm))
        w.record(*[f["properties"].get(fld[0].lower(), f["properties"].get(fld[0], "")) for fld in fields])
    w.close()
    for ext, buf in (("shp", shp), ("shx", shx), ("dbf", dbf)):
        zf.writestr(f"{name}.{ext}", buf.getvalue())
    zf.writestr(f"{name}.prj", _PRJ_WKT)


def export_zip(layers: dict) -> bytes:
    """layers: dict with keys lots/roads/reserve/open_space/parcel (GeoJSON FCs)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if layers.get("lots", {}).get("features"):
            _write_layer(zf, "lots", layers["lots"], shapefile.POLYGON, [
                ("id", "N", 10, 0), ("area_m2", "N", 12, 1), ("frontage_m", "N", 8, 1),
                ("compliant", "L", 1, 0),
            ])
        if layers.get("roads", {}).get("features"):
            _write_layer(zf, "road_centrelines", layers["roads"], shapefile.POLYLINE, [
                ("id", "N", 10, 0), ("length_m", "N", 10, 1), ("hierarchy", "C", 16, 0),
                ("reserve_m", "N", 6, 1),
            ])
        if layers.get("reserve", {}).get("features"):
            _write_layer(zf, "road_reserve", layers["reserve"], shapefile.POLYGON, [("kind", "C", 20, 0)])
        if layers.get("open_space", {}).get("features"):
            _write_layer(zf, "open_space", layers["open_space"], shapefile.POLYGON, [
                ("kind", "C", 20, 0), ("area_m2", "N", 12, 1)])
        if layers.get("parcel", {}).get("features"):
            _write_layer(zf, "parcel_boundary", layers["parcel"], shapefile.POLYGON, [("kind", "C", 20, 0)])
        zf.writestr("README.txt",
                    "Generated subdivision concept (not for construction).\n"
                    "CRS: GDA94 / MGA zone 55 (EPSG:28355).\n"
                    "Cadastral base: (c) State of Tasmania (the LIST).\n")
    return buf.getvalue()
