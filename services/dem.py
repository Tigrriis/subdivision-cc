"""Site terrain model from LIST vector contours (no GDAL required).

Sources (LIST Public/TopographyAndRelief MapServer, verified June 2026):
- layer 61: 0.25 m LiDAR contours (where covered) — used when available
- layer 13: 5 m contours (statewide)            — fallback
- layer 15: Rivers/Streams/Creeks [All]          — watercourse crossing warnings

Builds an IDW-interpolated elevation grid over the site (in EPSG:28355) and
answers elevation / gradient / contour-direction queries for the road engine.
"""
import math

import numpy as np
import requests
from pyproj import Transformer
from shapely.geometry import LineString, shape
from shapely.ops import transform

TOPO = "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/TopographyAndRelief/MapServer"
LAYER_CONTOUR_LIDAR = 61
LAYER_CONTOUR_5M = 13
LAYER_STREAMS = 15
TIMEOUT = 40

_TO_MGA = Transformer.from_crs(4326, 28355, always_xy=True)
_TO_WGS = Transformer.from_crs(28355, 4326, always_xy=True)

GRID_N = 56          # grid cells per axis
MAX_SRC_PTS = 2600   # cap on contour vertices used for interpolation
IDW_K = 8            # neighbours
MARGIN = 60.0        # m around parcel bbox


def _query_layer(layer_id, bbox_wgs, out_fields, max_records=1800):
    r = requests.get(f"{TOPO}/{layer_id}/query", params={
        "geometry": ",".join(f"{v:.6f}" for v in bbox_wgs),
        "geometryType": "esriGeometryEnvelope", "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects", "outFields": out_fields,
        "returnGeometry": "true", "outSR": "4326",
        "resultRecordCount": max_records, "f": "geojson",
    }, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("features", [])


class SiteTerrain:
    """Elevation model for one site. Use SiteTerrain.fetch(parcel_mga); may return None."""

    def __init__(self, x0, y0, x1, y1, grid, contours_wgs, source):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.grid = grid                # 2D elevation array [iy, ix]
        self.contours_wgs = contours_wgs  # geojson features for display
        self.source = source
        gy, gx = np.gradient(grid, (y1 - y0) / (GRID_N - 1), (x1 - x0) / (GRID_N - 1))
        self._gx, self._gy = gx, gy

    # ---------------- construction ----------------
    @classmethod
    def fetch(cls, parcel_mga):
        minx, miny, maxx, maxy = parcel_mga.bounds
        x0, y0, x1, y1 = minx - MARGIN, miny - MARGIN, maxx + MARGIN, maxy + MARGIN
        lon0, lat0 = _TO_WGS.transform(x0, y0)
        lon1, lat1 = _TO_WGS.transform(x1, y1)
        bbox = (min(lon0, lon1), min(lat0, lat1), max(lon0, lon1), max(lat0, lat1))

        feats, source = [], None
        try:
            feats = _query_layer(LAYER_CONTOUR_LIDAR, bbox, "ELEVATION")
            if feats:
                source = "LIST 0.25m LiDAR contours"
        except requests.RequestException:
            feats = []
        if not feats:
            try:
                feats = _query_layer(LAYER_CONTOUR_5M, bbox, "ELEVATION")
                source = "LIST 5m contours"
            except requests.RequestException:
                return None
        if not feats:
            return None

        pts, zs = [], []
        for f in feats:
            z = f["properties"].get("ELEVATION")
            if z is None:
                continue
            g = f["geometry"]
            lines = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]
            for line in lines:
                for lon, lat in line[::max(1, len(line) * len(feats) // (MAX_SRC_PTS * 4) or 1)]:
                    x, y = _TO_MGA.transform(lon, lat)
                    if x0 - MARGIN < x < x1 + MARGIN and y0 - MARGIN < y < y1 + MARGIN:
                        pts.append((x, y))
                        zs.append(float(z))
        if len(pts) < 8 or len(set(zs)) < 2:
            return None  # effectively flat / no data
        pts = np.asarray(pts)
        zs = np.asarray(zs)
        if len(pts) > MAX_SRC_PTS:
            idx = np.random.default_rng(11).choice(len(pts), MAX_SRC_PTS, replace=False)
            pts, zs = pts[idx], zs[idx]

        xs = np.linspace(x0, x1, GRID_N)
        ys = np.linspace(y0, y1, GRID_N)
        gx, gy = np.meshgrid(xs, ys)
        gpts = np.column_stack([gx.ravel(), gy.ravel()])
        # IDW with k nearest neighbours (numpy, chunked)
        grid = np.empty(len(gpts))
        for i in range(0, len(gpts), 800):
            chunk = gpts[i:i + 800]
            d2 = ((chunk[:, None, :] - pts[None, :, :]) ** 2).sum(axis=2)
            nn = np.argpartition(d2, IDW_K, axis=1)[:, :IDW_K]
            nd2 = np.take_along_axis(d2, nn, axis=1)
            w = 1.0 / np.maximum(nd2, 1.0)
            grid[i:i + 800] = (w * zs[nn]).sum(axis=1) / w.sum(axis=1)
        grid = grid.reshape(GRID_N, GRID_N)

        # simplified contours for the map display
        disp = []
        for f in feats[:400]:
            g = shape(f["geometry"]).simplify(0.00005)
            disp.append({"type": "Feature", "geometry": g.__geo_interface__,
                         "properties": {"elev": f["properties"].get("ELEVATION")}})
        return cls(x0, y0, x1, y1, grid, disp, source)

    # ---------------- queries (MGA coords) ----------------
    def elev(self, x, y):
        fx = (x - self.x0) / (self.x1 - self.x0) * (GRID_N - 1)
        fy = (y - self.y0) / (self.y1 - self.y0) * (GRID_N - 1)
        fx = min(max(fx, 0.0), GRID_N - 1.001)
        fy = min(max(fy, 0.0), GRID_N - 1.001)
        ix, iy = int(fx), int(fy)
        tx, ty = fx - ix, fy - iy
        g = self.grid
        return (g[iy, ix] * (1 - tx) * (1 - ty) + g[iy, ix + 1] * tx * (1 - ty)
                + g[iy + 1, ix] * (1 - tx) * ty + g[iy + 1, ix + 1] * tx * ty)

    def gradient(self, x, y):
        """(dz/dx, dz/dy) at point."""
        fx = min(max((x - self.x0) / (self.x1 - self.x0) * (GRID_N - 1), 0), GRID_N - 1.001)
        fy = min(max((y - self.y0) / (self.y1 - self.y0) * (GRID_N - 1), 0), GRID_N - 1.001)
        return float(self._gx[int(fy), int(fx)]), float(self._gy[int(fy), int(fx)])

    def slope_at(self, x, y):
        dx, dy = self.gradient(x, y)
        return math.hypot(dx, dy)

    def contour_angle(self, x, y):
        """Direction (deg, math convention) of the contour (perpendicular to gradient)."""
        dx, dy = self.gradient(x, y)
        return math.degrees(math.atan2(dy, dx)) + 90.0

    def mean_slope(self, poly, n=120):
        minx, miny, maxx, maxy = poly.bounds
        rng = np.random.default_rng(7)
        slopes = []
        for _ in range(n * 3):
            x = rng.uniform(minx, maxx)
            y = rng.uniform(miny, maxy)
            from shapely.geometry import Point
            if poly.contains(Point(x, y)):
                slopes.append(self.slope_at(x, y))
                if len(slopes) >= n:
                    break
        return float(np.mean(slopes)) if slopes else 0.0

    def grade_profile(self, line_m, spacing=20.0):
        """Max and mean |grade| along a line."""
        n = max(2, int(line_m.length / spacing) + 1)
        pts = [line_m.interpolate(i / (n - 1), normalized=True) for i in range(n)]
        grades = []
        for a, b in zip(pts[:-1], pts[1:]):
            d = a.distance(b)
            if d > 1:
                grades.append(abs(self.elev(b.x, b.y) - self.elev(a.x, a.y)) / d)
        return (max(grades), sum(grades) / len(grades)) if grades else (0.0, 0.0)

    def low_point(self, poly_boundary):
        """Lowest point on the parcel boundary — suggested stormwater discharge."""
        n = max(8, int(poly_boundary.length / 25))
        best, best_z = None, 1e9
        for i in range(n):
            p = poly_boundary.interpolate(i / n, normalized=True)
            z = self.elev(p.x, p.y)
            if z < best_z:
                best, best_z = p, z
        return best, best_z


def fetch_streams(parcel_mga):
    """Watercourse linestrings (MGA) intersecting the site bbox, for crossing warnings."""
    minx, miny, maxx, maxy = parcel_mga.bounds
    lon0, lat0 = _TO_WGS.transform(minx - 30, miny - 30)
    lon1, lat1 = _TO_WGS.transform(maxx + 30, maxy + 30)
    try:
        feats = _query_layer(LAYER_STREAMS, (min(lon0, lon1), min(lat0, lat1),
                                             max(lon0, lon1), max(lat0, lat1)),
                             "WATERCOURSE_TYPE", max_records=200)
    except requests.RequestException:
        return [], []
    geoms_m, display = [], []
    for f in feats:
        g = shape(f["geometry"])
        geoms_m.append(transform(_TO_MGA.transform, g))
        display.append({"type": "Feature", "geometry": f["geometry"],
                        "properties": {"kind": "watercourse"}})
    return geoms_m, display
