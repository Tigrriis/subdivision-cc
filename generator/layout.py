"""Core generative layout engine.

Pipeline (all geometry work in GDA94 / MGA zone 55, EPSG:28355, metres):
  1. Choose road grid orientation (user override or longest edge of the parcel's
     minimum rotated rectangle).
  2. Lay parallel road centrelines spaced for double-loaded blocks
     (2 x lot depth + reserve width), plus cross-streets to cap block length.
  3. Connect the network to the user's access points on the parcel boundary,
     prune unreachable segments, trim/flag dead ends and add cul-de-sac heads.
  4. Road reserve = buffered centrelines (+ head circles) clipped to the parcel.
  5. Blocks = parcel minus reserve; each block is sliced into lots
     (split along the block spine, then perpendicular cuts at frontage spacing).
  6. Compliance checks + statistics.
"""
import math

import numpy as np
from pyproj import Transformer
from shapely import affinity
from shapely.geometry import (
    GeometryCollection, LineString, MultiLineString, MultiPolygon, Point,
    Polygon, box, mapping, shape,
)
from shapely.ops import nearest_points, transform, unary_union

from .params import (
    CULDESAC_BUSHFIRE_MAX_LEN, CULDESAC_HEAD_RESERVE_RADIUS,
    CULDESAC_SHORT_MAX_LEN, GenParams, SOLAR_AXIS_TOLERANCE_DEG,
)

_TO_MGA = Transformer.from_crs(4326, 28355, always_xy=True)
_TO_WGS = Transformer.from_crs(28355, 4326, always_xy=True)

MIN_ROAD_PIECE = 25.0       # discard road fragments shorter than this
MIN_CULDESAC_LEN = 45.0     # dead-end stubs shorter than this get trimmed
NODE_SNAP = 0.5             # m, node coordinate snapping for graph building


def to_mga(geom):
    return transform(_TO_MGA.transform, geom)


def to_wgs(geom):
    return transform(_TO_WGS.transform, geom)


def _polys(geom):
    """Flatten any geometry into a list of Polygons."""
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


def _lines(geom):
    """Flatten any geometry into a list of LineStrings."""
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, (MultiLineString, GeometryCollection)):
        out = []
        for g in geom.geoms:
            out.extend(_lines(g))
        return out
    return []


def _auto_angle(parcel) -> float:
    """Angle (deg, math convention from +x) of the longest edge of the min rotated rect."""
    mrr = parcel.minimum_rotated_rectangle
    coords = list(mrr.exterior.coords)
    best, best_len = 0.0, -1.0
    for a, b in zip(coords[:-1], coords[1:]):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        if d > best_len:
            best_len = d
            best = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    return best % 180.0


def _segments(lines_union):
    """Break a noded union of lines into individual 2-point segments."""
    segs = []
    for ln in _lines(lines_union):
        cs = list(ln.coords)
        for a, b in zip(cs[:-1], cs[1:]):
            if math.hypot(b[0] - a[0], b[1] - a[1]) > 0.05:
                segs.append((a, b))
    return segs


def _node(pt):
    return (round(pt[0] / NODE_SNAP) * NODE_SNAP, round(pt[1] / NODE_SNAP) * NODE_SNAP)


def _build_network(lines, access_pts_m, parcel):
    """Node the lines, keep only segments reachable from an access point,
    iteratively trim short dead-end stubs. Returns (kept LineStrings, deadend nodes)."""
    if not lines:
        return [], []
    noded = unary_union(lines)
    segs = _segments(noded)
    if not segs:
        return [], []

    # adjacency
    def build_adj(segs):
        adj = {}
        for i, (a, b) in enumerate(segs):
            na, nb = _node(a), _node(b)
            adj.setdefault(na, []).append((i, nb))
            adj.setdefault(nb, []).append((i, na))
        return adj

    adj = build_adj(segs)

    # BFS from nodes nearest to each access point
    starts = set()
    nodes = list(adj.keys())
    for ap in access_pts_m:
        best = min(nodes, key=lambda n: (n[0] - ap.x) ** 2 + (n[1] - ap.y) ** 2)
        starts.add(best)
    if not starts and nodes:
        starts.add(nodes[0])

    seen_nodes, seen_segs = set(starts), set()
    queue = list(starts)
    while queue:
        n = queue.pop()
        for si, other in adj.get(n, []):
            seen_segs.add(si)
            if other not in seen_nodes:
                seen_nodes.add(other)
                queue.append(other)
    segs = [s for i, s in enumerate(segs) if i in seen_segs]

    # iteratively trim short dead-end chains (but never the access connectors)
    access_nodes = {_node((p.x, p.y)) for p in access_pts_m}
    boundary = parcel.exterior
    for _ in range(20):
        adj = build_adj(segs)
        deg = {n: len(v) for n, v in adj.items()}
        drop = set()
        for n, d in deg.items():
            if d != 1 or n in access_nodes:
                continue
            if boundary.distance(Point(n)) < 2.0:
                continue  # touches boundary (an access/extension), keep
            si, other = adj[n][0]
            a, b = segs[si]
            if math.hypot(b[0] - a[0], b[1] - a[1]) < MIN_CULDESAC_LEN and deg.get(other, 0) > 1:
                drop.add(si)
        if not drop:
            break
        segs = [s for i, s in enumerate(segs) if i not in drop]

    # final dead ends -> cul-de-sac heads
    adj = build_adj(segs)
    deadends = [n for n, v in adj.items()
                if len(v) == 1 and n not in access_nodes
                and boundary.distance(Point(n)) > CULDESAC_HEAD_RESERVE_RADIUS]
    kept = [LineString([a, b]) for a, b in segs]
    return kept, deadends


def _merge_lines(segs):
    """Merge contiguous collinear-ish segments back into longer linestrings for output."""
    if not segs:
        return []
    merged = unary_union(segs)
    from shapely.ops import linemerge
    m = linemerge(merged) if not isinstance(merged, LineString) else merged
    return _lines(m)


def _slice_strip(strip, w, min_area, max_area):
    """Cut a single-loaded strip into lots with vertical cuts every `w` metres."""
    lots = []
    for part in _polys(strip):
        if part.area < min_area * 0.5:
            lots.append(part)  # residue, merged/flagged later
            continue
        pminx, pminy, pmaxx, pmaxy = part.bounds
        n = max(1, round((pmaxx - pminx) / w))
        wx = (pmaxx - pminx) / n
        pending = None
        for i in range(n):
            cut = box(pminx + i * wx, pminy - 5, pminx + (i + 1) * wx + (0.01 if i == n - 1 else 0), pmaxy + 5)
            piece = part.intersection(cut)
            for pc in _polys(piece):
                if pending is not None:
                    pc = unary_union([pending, pc])
                    pc = max(_polys(pc), key=lambda g: g.area) if len(_polys(pc)) > 1 else _polys(pc)[0]
                    pending = None
                if pc.area < min_area:
                    pending = pc
                else:
                    lots.append(pc)
        if pending is not None:
            if lots and lots[-1].intersects(pending.buffer(0.1)):
                u = unary_union([lots.pop(), pending])
                lots.extend(_polys(u))
            else:
                lots.append(pending)
    return lots


def _subdivide_block(blk, lot_depth, target_area, min_area):
    """Split a block into lots. Double-loaded blocks are split along the spine first."""
    w = max(10.0, target_area / lot_depth)
    h = blk.bounds[3] - blk.bounds[1]
    halves = []
    if h > 1.6 * lot_depth:
        midy = (blk.bounds[1] + blk.bounds[3]) / 2.0
        bminx, bminy, bmaxx, bmaxy = blk.bounds
        halves = [blk.intersection(box(bminx - 5, bminy - 5, bmaxx + 5, midy)),
                  blk.intersection(box(bminx - 5, midy, bmaxx + 5, bmaxy + 5))]
    else:
        halves = [blk]
    lots = []
    for half in halves:
        lots.extend(_slice_strip(half, w, min_area, target_area * 2.2))
    return lots


def generate(parcel_geojson_geom: dict, params: GenParams) -> dict:
    """Main entry. parcel_geojson_geom: GeoJSON geometry dict (WGS84)."""
    zone = params.zone_preset()
    road = params.road()
    reserve = road["reserve"]
    lot_depth = params.depth()
    target_area = params.target_lot_area
    warnings = []

    parcel_wgs = shape(parcel_geojson_geom)
    parcel = to_mga(parcel_wgs)
    parcel = max(_polys(parcel.buffer(0)), key=lambda g: g.area)

    # --- orientation ----------------------------------------------------
    angle = params.road_angle_deg if params.road_angle_deg is not None else _auto_angle(parcel)
    origin = (parcel.centroid.x, parcel.centroid.y)
    rot_parcel = affinity.rotate(parcel, -angle, origin=origin)

    # SPP 8.6.1 A4: lot long axis (perpendicular to road) within 30 deg of true north.
    # Road direction bearing from north:
    road_bearing = (90.0 - angle) % 180.0
    lot_axis_bearing = (road_bearing + 90.0) % 180.0
    dev = min(lot_axis_bearing, 180.0 - lot_axis_bearing)
    if dev > SOLAR_AXIS_TOLERANCE_DEG:
        warnings.append(
            f"Lot long axis is {dev:.0f}° from true north (> {SOLAR_AXIS_TOLERANCE_DEG:.0f}°): "
            "relies on performance criterion P4 (SPP 8.6.1) for solar orientation.")

    # --- access points (snap to boundary) -------------------------------
    access_m = []
    for lon, lat in params.access_points:
        p = to_mga(Point(lon, lat))
        snapped = nearest_points(parcel.exterior, p)[0]
        access_m.append(affinity.rotate(snapped, -angle, origin=origin))
    if not access_m:
        warnings.append("No access points set — network connected at the western-most boundary point. "
                        "Click the boundary to set real access locations.")
        bx = min(parcel.exterior.coords, key=lambda c: c[0])
        access_m.append(affinity.rotate(Point(bx), -angle, origin=origin))

    # --- road grid in rotated frame -------------------------------------
    minx, miny, maxx, maxy = rot_parcel.bounds
    spacing = 2 * lot_depth + reserve
    inset = rot_parcel.buffer(-(reserve / 2 + 0.5))
    if inset.is_empty:
        raise ValueError("Parcel too small for an internal road reserve.")

    lines = []
    ys = np.arange(miny + lot_depth + reserve / 2, maxy - lot_depth * 0.6, spacing)
    if len(ys) == 0:
        ys = np.array([(miny + maxy) / 2.0])
    for y in ys:
        seg = LineString([(minx - 50, y), (maxx + 50, y)]).intersection(inset)
        lines.extend([l for l in _lines(seg) if l.length > MIN_ROAD_PIECE])

    # cross streets: connect rows & cap block length
    if len(ys) > 1 or (maxx - minx) > params.max_block_length:
        span = maxx - minx
        n_cross = max(1, int(span // params.max_block_length))
        xs = [minx + span * (i + 1) / (n_cross + 1) for i in range(n_cross)]
        # prefer a cross street aligned with the first access point
        if access_m:
            xs[0] = min(max(access_m[0].x, minx + lot_depth), maxx - lot_depth)
        for x in xs:
            seg = LineString([(x, miny - 50), (x, maxy + 50)]).intersection(inset)
            lines.extend([l for l in _lines(seg) if l.length > MIN_ROAD_PIECE])

    if not lines:
        raise ValueError("Could not fit any roads in the parcel with the current parameters.")

    # --- access connectors ----------------------------------------------
    grid_union = unary_union(lines)
    for ap in access_m:
        np_on_grid = nearest_points(grid_union, ap)[0]
        if np_on_grid.distance(ap) > 1.0:
            lines.append(LineString([(ap.x, ap.y), (np_on_grid.x, np_on_grid.y)]))

    # --- prune network ----------------------------------------------------
    kept, deadends = _build_network(lines, access_m, rot_parcel)
    if not kept:
        raise ValueError("Road network generation failed (no reachable roads).")

    # cul-de-sac length check (approx: chain length from dead end)
    merged_for_len = _merge_lines(kept)
    for ln in merged_for_len:
        for de in deadends:
            if Point(de).distance(Point(ln.coords[0])) < 1 or Point(de).distance(Point(ln.coords[-1])) < 1:
                if ln.length > CULDESAC_BUSHFIRE_MAX_LEN:
                    warnings.append(
                        f"Cul-de-sac ≈{ln.length:.0f} m exceeds 200 m — bushfire code C13.0 requires "
                        "7.0 m carriageway and other measures.")
                elif ln.length > CULDESAC_SHORT_MAX_LEN:
                    warnings.append(
                        f"Cul-de-sac ≈{ln.length:.0f} m exceeds 150 m — full 18 m reserve / footpaths "
                        "both sides required (TIG Table 4).")

    # --- road polygons -----------------------------------------------------
    res_parts = [l.buffer(reserve / 2, cap_style=2, join_style=2) for l in kept]
    res_parts += [Point(de).buffer(CULDESAC_HEAD_RESERVE_RADIUS) for de in deadends]
    reserve_poly = unary_union(res_parts).intersection(rot_parcel)
    carriageway_poly = unary_union(
        [l.buffer(road["carriageway"] / 2, cap_style=2, join_style=2) for l in kept]
        + [Point(de).buffer(9.0) for de in deadends]  # 18m dia head, face of kerb
    ).intersection(rot_parcel)

    # --- blocks & lots ------------------------------------------------------
    blocks = _polys(rot_parcel.difference(reserve_poly))
    lots, open_space = [], []
    res_edge = reserve_poly.buffer(0.15)
    for blk in blocks:
        if blk.area < zone.min_lot_area * 0.6:
            open_space.append(blk)
            continue
        for lot in _subdivide_block(blk, lot_depth, target_area, zone.min_lot_area):
            frontage = lot.exterior.intersection(res_edge).length if isinstance(lot, Polygon) else 0
            if lot.area < zone.min_lot_area or frontage < zone.min_frontage:
                # try to keep as lot if frontage just under; else open space
                if lot.area >= zone.min_lot_area and frontage >= zone.min_frontage * 0.75:
                    lots.append((lot, frontage, False))
                else:
                    open_space.append(lot)
            else:
                lots.append((lot, frontage, True))

    # --- envelope (10x15 @ <=1:5) approximate check ---------------------------
    lot_records = []
    for i, (lot, frontage, ok) in enumerate(lots, start=1):
        eroded = lot.buffer(-min(zone.side_setback, zone.rear_setback) - 0.5)
        env_ok = False
        if not eroded.is_empty:
            for ep in _polys(eroded):
                mrr = ep.minimum_rotated_rectangle
                cs = list(mrr.exterior.coords)
                d1 = math.hypot(cs[1][0] - cs[0][0], cs[1][1] - cs[0][1])
                d2 = math.hypot(cs[2][0] - cs[1][0], cs[2][1] - cs[1][1])
                if min(d1, d2) >= zone.envelope_w and max(d1, d2) >= zone.envelope_d:
                    env_ok = True
                    break
        lot_records.append({
            "id": i, "geom": lot, "area": lot.area, "frontage": frontage,
            "frontage_ok": frontage >= zone.min_frontage,
            "area_ok": lot.area >= zone.min_lot_area,
            "envelope_ok": env_ok,
        })

    non_env = [r["id"] for r in lot_records if not r["envelope_ok"]]
    if non_env:
        warnings.append(
            f"{len(non_env)} lot(s) may not fit the {zone.envelope_w:.0f}×{zone.envelope_d:.0f} m "
            f"building envelope clear of setbacks (ids: {non_env[:12]}{'…' if len(non_env) > 12 else ''}).")
    non_frontage = [r["id"] for r in lot_records if not r["frontage_ok"]]
    if non_frontage:
        warnings.append(
            f"{len(non_frontage)} lot(s) below the {zone.min_frontage} m frontage Acceptable Solution "
            f"(ids: {non_frontage[:12]}{'…' if len(non_frontage) > 12 else ''}).")

    n_lots = len(lot_records)
    if n_lots > 100:
        warnings.append("More than 100 lots — a traffic impact assessment and a collector street "
                        "(11.0 m carriageway / 20 m reserve) are likely required for the spine road.")

    # --- de-rotate, reproject, serialise ------------------------------------
    def unrot(g):
        return affinity.rotate(g, angle, origin=origin)

    def fc(features):
        return {"type": "FeatureCollection", "features": features}

    lot_feats = []
    for r in lot_records:
        g = unrot(r["geom"])
        lot_feats.append({
            "type": "Feature",
            "geometry": mapping(to_wgs(g)),
            "properties": {
                "id": r["id"], "area_m2": round(r["area"], 1),
                "frontage_m": round(r["frontage"], 1),
                "compliant": bool(r["area_ok"] and r["frontage_ok"] and r["envelope_ok"]),
                "area_ok": bool(r["area_ok"]), "frontage_ok": bool(r["frontage_ok"]),
                "envelope_ok": bool(r["envelope_ok"]),
            },
        })

    road_feats = []
    total_road_len = 0.0
    for i, ln in enumerate(_merge_lines(kept), start=1):
        g = unrot(ln)
        total_road_len += ln.length
        road_feats.append({
            "type": "Feature", "geometry": mapping(to_wgs(g)),
            "properties": {"id": i, "length_m": round(ln.length, 1),
                           "hierarchy": "local", "reserve_m": reserve,
                           "carriageway_m": road["carriageway"]},
        })

    reserve_feats = [{"type": "Feature", "geometry": mapping(to_wgs(unrot(p))),
                      "properties": {"kind": "road_reserve"}} for p in _polys(reserve_poly)]
    carriageway_feats = [{"type": "Feature", "geometry": mapping(to_wgs(unrot(p))),
                          "properties": {"kind": "carriageway"}} for p in _polys(carriageway_poly)]
    os_feats = [{"type": "Feature", "geometry": mapping(to_wgs(unrot(p))),
                 "properties": {"kind": "open_space", "area_m2": round(p.area, 1)}}
                for p in _polys(unary_union(open_space)) if p.area > 5] if open_space else []

    areas = [r["area"] for r in lot_records] or [0]
    os_area = sum(p.area for p in open_space)
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
        "zone": zone.code,
    }

    parcel_feat = {"type": "Feature", "geometry": mapping(parcel_wgs), "properties": {"kind": "parcel"}}

    return {
        "lots": fc(lot_feats),
        "roads": fc(road_feats),
        "reserve": fc(reserve_feats),
        "carriageway": fc(carriageway_feats),
        "open_space": fc(os_feats),
        "parcel": fc([parcel_feat]),
        "stats": stats,
        "warnings": warnings,
    }
