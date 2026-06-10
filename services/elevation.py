"""Elevation sampling and road grade checking.

v1 uses the public OpenTopoData SRTM 30 m API (no key, 100 points/request,
1 request/second). Indicative only — swap ENDPOINT for a LiDAR DEM service or
user-uploaded DEM for detailed design.
"""
import time

import requests
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

from generator.params import MAX_ROAD_GRADE, TARGET_ROAD_GRADE

ENDPOINT = "https://api.opentopodata.org/v1/srtm30m"
SAMPLE_SPACING = 30.0  # m along road
MAX_POINTS = 300       # cap total samples per request to stay polite

_TO_MGA = Transformer.from_crs(4326, 28355, always_xy=True)
_TO_WGS = Transformer.from_crs(28355, 4326, always_xy=True)


def _sample_points(line_m, spacing):
    n = max(2, int(line_m.length / spacing) + 1)
    return [line_m.interpolate(i / (n - 1), normalized=True) for i in range(n)]


def _fetch_elevations(lonlats):
    out = []
    for i in range(0, len(lonlats), 100):
        chunk = lonlats[i:i + 100]
        locs = "|".join(f"{lat:.6f},{lon:.6f}" for lon, lat in chunk)
        r = requests.get(ENDPOINT, params={"locations": locs}, timeout=30)
        r.raise_for_status()
        d = r.json()
        if d.get("status") != "OK":
            raise RuntimeError(d.get("error", "elevation service error"))
        out.extend([res["elevation"] for res in d["results"]])
        if i + 100 < len(lonlats):
            time.sleep(1.1)  # API rate limit
    return out


def check_road_grades(roads_fc: dict) -> dict:
    """roads_fc: GeoJSON FeatureCollection of centrelines (WGS84).
    Returns per-road max grade and compliance flags."""
    roads = []
    total_pts = 0
    for f in roads_fc.get("features", []):
        line_wgs = shape(f["geometry"])
        line_m = transform(_TO_MGA.transform, line_wgs)
        pts_m = _sample_points(line_m, SAMPLE_SPACING)
        if total_pts + len(pts_m) > MAX_POINTS:
            pts_m = pts_m[: max(2, MAX_POINTS - total_pts)]
        total_pts += len(pts_m)
        lonlats = [_TO_WGS.transform(p.x, p.y) for p in pts_m]
        roads.append({"id": f["properties"].get("id"), "pts_m": pts_m, "lonlats": lonlats})

    all_lonlats = [ll for r in roads for ll in r["lonlats"]]
    elevs = _fetch_elevations(all_lonlats)

    idx = 0
    report, warnings = [], []
    for r in roads:
        n = len(r["lonlats"])
        ev = elevs[idx: idx + n]
        idx += n
        grades = []
        for i in range(1, n):
            d = r["pts_m"][i].distance(r["pts_m"][i - 1])
            if d > 1 and ev[i] is not None and ev[i - 1] is not None:
                grades.append(abs(ev[i] - ev[i - 1]) / d)
        max_g = max(grades) if grades else 0.0
        ok = max_g <= MAX_ROAD_GRADE
        report.append({
            "id": r["id"], "max_grade_pct": round(max_g * 100, 1),
            "ok": ok, "soft_ok": max_g <= TARGET_ROAD_GRADE,
        })
        if not ok:
            warnings.append(f"Road {r['id']}: max grade {max_g * 100:.1f}% exceeds 17% limit (TIG 3.4.5).")
        elif max_g > TARGET_ROAD_GRADE:
            warnings.append(f"Road {r['id']}: max grade {max_g * 100:.1f}% exceeds the 10% bus/heavy-vehicle target.")

    return {"available": True, "source": "SRTM 30m (indicative only)",
            "roads": report, "warnings": warnings}
