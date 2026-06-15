"""Layout orchestration.

generate(parcel_geometry | [geometries], params, roads_override=None) ->
GeoJSON layers + stats + warnings.

Pipeline (EPSG:28355): fetch terrain (LIST contours) -> choose orientation
(contour/solar/long-axis scoring) -> straight grid OR user-edited centrelines ->
prune to access points -> contour-following warp -> reserve buffers -> blocks ->
frontage-perpendicular lot cutting -> compliance + stats.

Design rationale: see DESIGN_PRINCIPLES.md; standards: STANDARDS_REFERENCE.md.
"""
import math

from pyproj import Transformer
from shapely.geometry import (GeometryCollection, LineString, MultiPolygon,
                              Point, Polygon, mapping, shape)
from shapely.ops import nearest_points, transform, unary_union

import numpy as np

from services.dem import SiteTerrain, fetch_streams
from . import lots as lots_mod
from . import roads as roads_mod
from .params import (CULDESAC_BUSHFIRE_MAX_LEN, CULDESAC_HEAD_RESERVE_RADIUS,
                     CULDESAC_SHORT_MAX_LEN, GenParams, MAX_ROAD_GRADE,
                     SOLAR_AXIS_TOLERANCE_DEG, TARGET_ROAD_GRADE)

_TO_MGA = Transformer.from_crs(4326, 28355, always_xy=True)
_TO_WGS = Transformer.from_crs(28355, 4326, always_xy=True)


def to_mga(geom):
    return transform(_TO_MGA.transform, geom)


def to_wgs(geom):
    return transform(_TO_WGS.transform, geom)


def _polys(geom):
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, (MultiPolygon, GeometryCollection)):
        out = []
        for g in geom.geoms:
            out.extend(_polys(g))
        return out
    return []


def _fc(features):
    return {"type": "FeatureCollection", "features": features}


def _dissolve_parcels(parcel_geoms):
    """One or more GeoJSON geometries -> single MGA polygon (must be contiguous)."""
    geoms = parcel_geoms if isinstance(parcel_geoms, list) else [parcel_geoms]
    shapes_wgs = [shape(g) for g in geoms]
    merged = unary_union([to_mga(s).buffer(0.05) for s in shapes_wgs]).buffer(-0.05)
    parts = _polys(merged)
    if not parts:
        raise ValueError("Invalid parcel geometry.")
    if len(parts) > 1:
        raise ValueError("Selected parcels are not contiguous — remove the detached one(s).")
    return parts[0], unary_union(shapes_wgs)


_SITE_CACHE: dict = {}


def _site_context(parcel):
    """Fetch (or reuse) terrain + watercourses for a site. Keyed by rounded bounds so
    the road-tweaking loop doesn't re-hit LIST on every regeneration."""
    key = tuple(round(v, -1) for v in parcel.bounds)
    if key in _SITE_CACHE:
        return _SITE_CACHE[key]
    terrain = None
    note = "unavailable"
    try:
        terrain = SiteTerrain.fetch(parcel)
        note = terrain.source if terrain else "no contour data (site treated as flat)"
    except Exception:
        note = "terrain service error (site treated as flat)"
    try:
        streams_m, streams_fc = fetch_streams(parcel)
    except Exception:
        streams_m, streams_fc = [], []
    if len(_SITE_CACHE) > 24:
        _SITE_CACHE.clear()
    _SITE_CACHE[key] = (terrain, note, streams_m, streams_fc)
    return _SITE_CACHE[key]


def generate(parcel_geoms, params: GenParams, roads_override: dict | None = None) -> dict:
    zone = params.zone_preset()
    road = params.road()
    reserve = road["reserve"]
    lot_depth = params.depth()
    target_area = params.target_lot_area
    warnings = []

    parcel, parcel_wgs = _dissolve_parcels(parcel_geoms)

    # ---------------- terrain (cached per site, graceful failure) ----------------
    terrain, terrain_note, streams_m, streams_fc = _site_context(parcel)

    # ---------------- access points ----------------
    access_m = []
    for lon, lat in params.access_points:
        p = to_mga(Point(lon, lat))
        access_m.append(nearest_points(parcel.exterior, p)[0])
    if not access_m:
        warnings.append("No access points set — network connected at the western-most boundary "
                        "point. Click the boundary to set real access locations.")
        bx = min(parcel.exterior.coords, key=lambda c: c[0])
        access_m.append(Point(bx))

    # ---------------- road network ----------------
    if roads_override and roads_override.get("features"):
        lines = []
        for f in roads_override["features"]:
            g = to_mga(shape(f["geometry"]))
            lines.extend([g] if isinstance(g, LineString) else list(getattr(g, "geoms", [])))
        # clip to parcel, keep topology as drawn (no warping of user edits)
        lines = [l.intersection(parcel) for l in lines]
        lines = [p for l in lines for p in ([l] if isinstance(l, LineString) else list(getattr(l, "geoms", [])))
                 if isinstance(p, LineString) and p.length > 5]
        if not lines:
            raise ValueError("Edited roads fall outside the parcel.")
        angle = params.road_angle_deg if params.road_angle_deg is not None else roads_mod._auto_angle_obb(parcel)
        angle_note = "user-edited alignment"
        segs, deadends = roads_mod.prune_network(lines, access_m, parcel)
        chains = roads_mod.chains(segs)
    else:
        pattern = params.street_pattern
        if params.road_angle_deg is not None:
            angle, angle_note = params.road_angle_deg, "user bearing"
        else:
            angle, angle_note = roads_mod.choose_angle(
                parcel, terrain, lot_depth, reserve, params.max_block_length, access_m,
                target_area=target_area)
        angle_note = f"{pattern} · {angle_note}"
        lines = roads_mod.build_network(parcel, pattern, angle, lot_depth, reserve,
                                        params.max_block_length, access_m)
        segs, deadends = roads_mod.prune_network(lines, access_m, parcel)
        if not segs:
            raise ValueError("Road network generation failed (no reachable roads).")
        chains = roads_mod.chains(segs)
        # rectilinear stays straight; radiant is already curved; modified/organic
        # follow contours (organic warps harder).
        if pattern in ("modified", "organic"):
            max_dev = (0.22 if pattern == "modified" else 0.35) * lot_depth
            chains = roads_mod.warp_chains(chains, parcel, terrain, max_dev=max_dev)
            segs = roads_mod._segments(unary_union(chains))
            _, deadends = roads_mod.prune_network(chains, access_m, parcel)

    if not chains:
        raise ValueError("Road network generation failed.")

    # solar orientation note (SPP 8.6.1 A4)
    road_bearing = (90.0 - angle) % 180.0
    lot_axis_bearing = (road_bearing + 90.0) % 180.0
    dev = min(lot_axis_bearing, 180.0 - lot_axis_bearing)
    if dev > SOLAR_AXIS_TOLERANCE_DEG:
        warnings.append(f"Primary lot axis ≈{dev:.0f}° from true north (>30°): relies on "
                        "performance criterion P4 (SPP 8.6.1) for solar orientation.")

    # cul-de-sac lengths
    for ln in chains:
        ends = [ln.coords[0], ln.coords[-1]]
        for de in deadends:
            if any(math.hypot(de[0] - e[0], de[1] - e[1]) < 1.5 for e in ends):
                if ln.length > CULDESAC_BUSHFIRE_MAX_LEN:
                    warnings.append(f"Cul-de-sac ≈{ln.length:.0f} m exceeds 200 m — bushfire code "
                                    "C13.0 requires 7.0 m carriageway and other measures.")
                elif ln.length > CULDESAC_SHORT_MAX_LEN:
                    warnings.append(f"Cul-de-sac ≈{ln.length:.0f} m exceeds 150 m — full 18 m "
                                    "reserve / footpaths both sides required (TIG Table 4).")
                break

    # watercourse crossings
    if streams_m:
        n_cross = sum(1 for ln in chains for s in streams_m if ln.intersects(s))
        if n_cross:
            warnings.append(f"Road network crosses mapped watercourse(s) at ~{n_cross} location(s) "
                            "— culvert/bridge and Natural Assets Code C7.0 apply.")

    # ---------------- road polygons ----------------
    res_parts = [l.buffer(reserve / 2, cap_style=2, join_style=1) for l in chains]
    res_parts += [Point(de).buffer(CULDESAC_HEAD_RESERVE_RADIUS) for de in deadends]
    reserve_poly = unary_union(res_parts).intersection(parcel)
    carriageway_poly = unary_union(
        [l.buffer(road["carriageway"] / 2, cap_style=2, join_style=1) for l in chains]
        + [Point(de).buffer(9.0) for de in deadends]).intersection(parcel)

    # ---------------- blocks & lots ----------------
    reserve_edge = reserve_poly.boundary
    blocks = _polys(parcel.difference(reserve_poly))
    rng = np.random.default_rng(int(parcel.area) % 100000)
    lot_geoms, open_space = [], []
    for blk in blocks:
        if blk.area < zone.min_lot_area * 0.6:
            open_space.append(blk)
            continue
        got, os_parts = lots_mod.subdivide_block_recursive(
            blk, reserve_edge, lot_depth, target_area, zone.min_lot_area,
            zone.min_frontage, rng)
        lot_geoms.extend(got)
        open_space.extend(os_parts)

    # ---------------- per-lot compliance ----------------
    lot_records = []
    for i, (lot, frontage) in enumerate(sorted(lot_geoms, key=lambda t: (-t[0].centroid.y, t[0].centroid.x)), 1):
        eroded = lot.buffer(-min(zone.side_setback, zone.rear_setback) - 0.5)
        env_ok = False
        for ep in _polys(eroded):
            mrr = ep.minimum_rotated_rectangle
            cs = list(mrr.exterior.coords)
            d1 = math.hypot(cs[1][0] - cs[0][0], cs[1][1] - cs[0][1])
            d2 = math.hypot(cs[2][0] - cs[1][0], cs[2][1] - cs[1][1])
            if min(d1, d2) >= zone.envelope_w and max(d1, d2) >= zone.envelope_d:
                env_ok = True
                break
        slope_ok = True
        if terrain is not None:
            c = lot.representative_point()
            slope_ok = terrain.slope_at(c.x, c.y) <= zone.max_envelope_slope
        lot_records.append({
            "id": i, "geom": lot, "area": lot.area, "frontage": frontage,
            "area_ok": lot.area >= zone.min_lot_area,
            "frontage_ok": frontage >= zone.min_frontage,
            "envelope_ok": env_ok and slope_ok,
            "steep": not slope_ok,
        })

    for key, msg in (("envelope_ok", f"may not fit the {zone.envelope_w:.0f}×{zone.envelope_d:.0f} m "
                                     "building envelope clear of setbacks (or core >1:5 slope)"),
                     ("frontage_ok", f"below the {zone.min_frontage} m frontage Acceptable Solution"),
                     ("area_ok", f"below the {zone.min_lot_area:.0f} m² minimum area")):
        bad = [r["id"] for r in lot_records if not r[key]]
        if bad:
            warnings.append(f"{len(bad)} lot(s) {msg} (ids: {bad[:12]}{'…' if len(bad) > 12 else ''}).")

    n_lots = len(lot_records)
    if n_lots > 100:
        warnings.append("More than 100 lots — traffic impact assessment and a collector street "
                        "(11.0 m / 20 m reserve) likely required for the spine road.")

    # ---------------- road grades from terrain ----------------
    grade_report = []
    road_feats, total_road_len = [], 0.0
    for i, ln in enumerate(chains, 1):
        total_road_len += ln.length
        props = {"id": i, "length_m": round(ln.length, 1), "hierarchy": "local",
                 "reserve_m": reserve, "carriageway_m": road["carriageway"]}
        if terrain is not None:
            gmax, gmean = terrain.grade_profile(ln)
            gmax = float(gmax)
            props["max_grade_pct"] = round(gmax * 100, 1)
            grade_report.append({"id": i, "max_grade_pct": props["max_grade_pct"],
                                 "ok": bool(gmax <= MAX_ROAD_GRADE),
                                 "soft_ok": bool(gmax <= TARGET_ROAD_GRADE)})
            if gmax > MAX_ROAD_GRADE:
                warnings.append(f"Road {i}: max grade {gmax * 100:.0f}% exceeds the 17% limit "
                                "(TIG 3.4.5) — realign or accept retaining/benching.")
            elif gmax > TARGET_ROAD_GRADE:
                warnings.append(f"Road {i}: max grade {gmax * 100:.0f}% exceeds the 10% "
                                "bus/heavy-vehicle target (TIG 3.4.5).")
        # simplify slightly so the editor shows a manageable number of drag handles
        road_feats.append({"type": "Feature", "geometry": mapping(to_wgs(ln.simplify(0.4))),
                           "properties": props})

    # ---------------- serialise ----------------
    lot_feats = [{
        "type": "Feature", "geometry": mapping(to_wgs(r["geom"])),
        "properties": {"id": r["id"], "area_m2": round(r["area"], 1),
                       "frontage_m": round(r["frontage"], 1),
                       "compliant": bool(r["area_ok"] and r["frontage_ok"] and r["envelope_ok"]),
                       "area_ok": bool(r["area_ok"]), "frontage_ok": bool(r["frontage_ok"]),
                       "envelope_ok": bool(r["envelope_ok"]), "steep": bool(r["steep"])},
    } for r in lot_records]

    reserve_feats = [{"type": "Feature", "geometry": mapping(to_wgs(p)),
                      "properties": {"kind": "road_reserve"}} for p in _polys(reserve_poly)]
    carriageway_feats = [{"type": "Feature", "geometry": mapping(to_wgs(p)),
                          "properties": {"kind": "carriageway"}} for p in _polys(carriageway_poly)]
    os_union = unary_union(open_space) if open_space else None
    os_feats = [{"type": "Feature", "geometry": mapping(to_wgs(p)),
                 "properties": {"kind": "open_space", "area_m2": round(p.area, 1)}}
                for p in (_polys(os_union) if os_union else []) if p.area > 5]

    extras = []
    if terrain is not None:
        lp, lz = terrain.low_point(parcel.exterior)
        extras.append({"type": "Feature", "geometry": mapping(to_wgs(lp)),
                       "properties": {"kind": "suggested_discharge",
                                      "elev_m": float(round(lz, 1)),
                                      "note": "lowest boundary point — candidate stormwater discharge/detention"}})

    areas = [r["area"] for r in lot_records] or [0]
    os_area = sum(p.area for p in open_space) if open_space else 0
    stats = {
        "lot_count": n_lots,
        "compliant_lots": sum(1 for r in lot_records
                              if r["area_ok"] and r["frontage_ok"] and r["envelope_ok"]),
        "min_lot_area": round(min(areas), 0), "max_lot_area": round(max(areas), 0),
        "avg_lot_area": round(sum(areas) / max(1, n_lots), 0),
        "total_road_length_m": round(total_road_len, 0),
        "road_reserve_area_m2": round(reserve_poly.area, 0),
        "open_space_area_m2": round(os_area, 0),
        "parcel_area_m2": round(parcel.area, 0),
        "parcel_area_ha": round(parcel.area / 10000.0, 2),
        "yield_per_ha": round(n_lots / (parcel.area / 10000.0), 1) if parcel.area else 0,
        "road_area_pct": round(100.0 * reserve_poly.area / parcel.area, 1),
        "road_angle_deg": round(angle, 1),
        "orientation_basis": angle_note,
        "connectivity_index": roads_mod.connectivity_index(segs),
        "mean_slope_pct": float(round(terrain.mean_slope(parcel) * 100, 1)) if terrain else None,
        "terrain_source": terrain_note,
        "zone": zone.code,
    }

    return {
        "lots": _fc(lot_feats),
        "roads": _fc(road_feats),
        "reserve": _fc(reserve_feats),
        "carriageway": _fc(carriageway_feats),
        "open_space": _fc(os_feats),
        "parcel": _fc([{"type": "Feature", "geometry": mapping(parcel_wgs),
                        "properties": {"kind": "parcel"}}]),
        "contours": _fc(terrain.contours_wgs if terrain else []),
        "streams": _fc(streams_fc),
        "extras": _fc(extras),
        "grades": grade_report,
        "stats": stats,
        "warnings": warnings,
    }
