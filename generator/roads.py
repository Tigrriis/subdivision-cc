"""Road network generation: oriented grid -> access connection -> pruning ->
contour-following warp (or gentle aesthetic curvature on flat sites).

All geometry in EPSG:28355 metres. Junction nodes are never moved by warping,
so network topology survives editing/regeneration.
"""
import math

import numpy as np
from shapely import affinity
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge, nearest_points, unary_union

from .params import CULDESAC_HEAD_RESERVE_RADIUS, SOLAR_AXIS_TOLERANCE_DEG

MIN_ROAD_PIECE = 25.0
MIN_CULDESAC_LEN = 45.0
NODE_SNAP = 0.5
WARP_SAMPLE = 18.0          # m between samples when warping
WARP_SLOPE_THRESHOLD = 0.05  # warp only if mean slope above this
FLAT_CURVE_AMP = 4.0        # m amplitude of aesthetic curvature
SMOOTH_PASSES = 2


def _lines(geom):
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if hasattr(geom, "geoms"):
        out = []
        for g in geom.geoms:
            out.extend(_lines(g))
        return out
    return []


def _node(pt):
    return (round(pt[0] / NODE_SNAP) * NODE_SNAP, round(pt[1] / NODE_SNAP) * NODE_SNAP)


def _segments(lines_union):
    segs = []
    for ln in _lines(lines_union):
        cs = list(ln.coords)
        for a, b in zip(cs[:-1], cs[1:]):
            if math.hypot(b[0] - a[0], b[1] - a[1]) > 0.05:
                segs.append((a, b))
    return segs


# --------------------------------------------------------------- orientation
def _auto_angle_obb(parcel):
    mrr = parcel.minimum_rotated_rectangle
    coords = list(mrr.exterior.coords)
    best, best_len = 0.0, -1.0
    for a, b in zip(coords[:-1], coords[1:]):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        if d > best_len:
            best_len, best = d, math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    return best % 180.0


def choose_angle(parcel, terrain, lot_depth, reserve, max_block_len, access_m,
                 target_area=600.0):
    """Pick the road bearing by TRIAL-BUILDING the network at candidate angles and
    scoring what actually results: estimated lot yield (dominant — balances too few
    roads against over-roading, since yield is frontage-limited when roads are scarce
    and area-limited when roads eat the site), contour alignment of the built roads
    (hillside principle), and solar-friendly lot axes (SPP 8.6.1 A4)."""
    obb = _auto_angle_obb(parcel)
    mean_slope = terrain.mean_slope(parcel) if terrain is not None else 0.0
    contour_w = 0.0 if mean_slope < WARP_SLOPE_THRESHOLD else min(1.5, 18.0 * mean_slope)

    w_front = max(10.0, target_area / lot_depth)
    max_lots = max(parcel.area / target_area, 1.0)

    best_angle, best_score, best_note = obb, -1e9, "parcel long axis"
    for cand in list(range(0, 180, 15)) + [int(obb) % 180]:
        try:
            lines = straight_grid(parcel, float(cand), lot_depth, reserve, max_block_len, access_m)
            segs, _ = prune_network(lines, access_m, parcel)
        except ValueError:
            continue
        if not segs:
            continue
        net_len = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in segs)
        est_frontage_lots = 2.0 * net_len / w_front           # if every road double-loaded
        est_area_lots = max(0.0, (parcel.area - net_len * reserve) / target_area)
        length_score = min(est_frontage_lots, est_area_lots) / max_lots

        contour_score = 0.0
        if contour_w > 0:
            cos2 = []
            for a, b in segs[::max(1, len(segs) // 40)]:
                seg_ang = math.atan2(b[1] - a[1], b[0] - a[0])
                mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                if terrain.slope_at(mx, my) > 0.03:
                    ca = math.radians(terrain.contour_angle(mx, my))
                    cos2.append(abs(math.cos(seg_ang - ca)))
            contour_score = float(np.mean(cos2)) if cos2 else 0.0

        lot_axis_bearing = (90.0 - (cand + 90.0)) % 180.0
        solar_dev = min(lot_axis_bearing, 180.0 - lot_axis_bearing)
        solar_score = max(0.0, 1.0 - max(0.0, solar_dev - SOLAR_AXIS_TOLERANCE_DEG) / 60.0)

        score = 2.0 * length_score + contour_w * contour_score + 0.35 * solar_score
        if score > best_score:
            best_score, best_angle = score, float(cand)
            best_note = (f"contour-aware fit (mean slope {mean_slope * 100:.0f}%)"
                         if contour_w > 0 else "best network fit")
    return best_angle, best_note


# --------------------------------------------------------------- patterns
def build_network(parcel, pattern, angle, lot_depth, reserve, max_block_len, access_m):
    """Dispatch to a street-pattern generator. Returns road centrelines (MGA)."""
    if pattern == "radiant":
        focal = access_m[0] if access_m else parcel.representative_point()
        return radiant_grid(parcel, focal, lot_depth, reserve, max_block_len, access_m)
    stagger = (pattern == "modified")
    return straight_grid(parcel, angle, lot_depth, reserve, max_block_len, access_m,
                         stagger=stagger)


def straight_grid(parcel, angle, lot_depth, reserve, max_block_len, access_m, stagger=False):
    """Straight skeleton of the network in MGA coords (built in a rotated frame).
    `stagger` offsets alternate cross streets (modified grid) for T-intersections."""
    origin = (parcel.centroid.x, parcel.centroid.y)
    rot = affinity.rotate(parcel, -angle, origin=origin)
    minx, miny, maxx, maxy = rot.bounds
    spacing = 2 * lot_depth + reserve
    inset = rot.buffer(-(reserve / 2 + 0.5))
    if inset.is_empty:
        raise ValueError("Parcel too small for an internal road reserve.")

    lines = []
    ys = np.arange(miny + lot_depth + reserve / 2, maxy - lot_depth * 0.6, spacing)
    if len(ys) == 0:
        ys = np.array([(miny + maxy) / 2.0])
    for y in ys:
        seg = LineString([(minx - 50, y), (maxx + 50, y)]).intersection(inset)
        lines.extend([l for l in _lines(seg) if l.length > MIN_ROAD_PIECE])

    access_rot = [affinity.rotate(p, -angle, origin=origin) for p in access_m]
    if len(ys) > 1 or (maxx - minx) > max_block_len:
        span = maxx - minx
        n_cross = max(1, int(span // max_block_len))
        xs = [minx + span * (i + 1) / (n_cross + 1) for i in range(n_cross)]
        if access_rot:
            xs[0] = min(max(access_rot[0].x, minx + lot_depth), maxx - lot_depth)
        for j, x in enumerate(xs):
            if stagger and j % 2 == 1:
                # break alternate cross streets at the midline -> T-intersections,
                # keeping continuous through-streets in the primary direction
                mid = (miny + maxy) / 2.0
                for y0, y1 in ((miny - 50, mid + reserve / 2), (mid - reserve / 2, maxy + 50)):
                    if (j // 2) % 2 == 0 and y0 < mid:
                        continue
                    if (j // 2) % 2 == 1 and y0 >= mid:
                        continue
                    seg = LineString([(x, y0), (x, y1)]).intersection(inset)
                    lines.extend([l for l in _lines(seg) if l.length > MIN_ROAD_PIECE])
            else:
                seg = LineString([(x, miny - 50), (x, maxy + 50)]).intersection(inset)
                lines.extend([l for l in _lines(seg) if l.length > MIN_ROAD_PIECE])
    if not lines:
        raise ValueError("Could not fit any roads in the parcel with the current parameters.")

    lines = _connect_components(lines, rot, reserve)

    grid_union = unary_union(lines)
    for ap in access_rot:
        npt = nearest_points(grid_union, ap)[0]
        if npt.distance(ap) > 1.0:
            lines.append(LineString([(ap.x, ap.y), (npt.x, npt.y)]))
    return [affinity.rotate(l, angle, origin=origin) for l in lines]


def radiant_grid(parcel, focal, lot_depth, reserve, max_block_len, access_m):
    """Concentric ring streets + radial spokes about a focal point (e.g. the gateway).
    Per MBRC, radiant grids respond to a focal point and transition outward."""
    cx, cy = focal.x, focal.y
    inset = parcel.buffer(-(reserve / 2 + 0.5))
    if inset.is_empty:
        raise ValueError("Parcel too small for an internal road reserve.")
    verts = list(parcel.exterior.coords)
    maxr = max(math.hypot(x - cx, y - cy) for x, y in verts)
    spacing = 2 * lot_depth + reserve

    # start rings beyond an inner radius so blocks near the focal point are not tiny
    r0 = max(spacing, 1.5 * lot_depth)
    lines = []
    r = r0
    radii = []
    while r < maxr + spacing:
        ring = Point(cx, cy).buffer(r, resolution=72).exterior
        seg = ring.intersection(inset)
        lines.extend([l for l in _lines(seg) if l.length > MIN_ROAD_PIECE])
        radii.append(r)
        r += spacing
    # radial spokes across the parcel's actual angular extent from the focal point
    # (a focal point on the boundary only subtends part of a full circle)
    angs = np.sort(np.array([math.atan2(y - cy, x - cx) for x, y in verts[:-1]]))
    gaps = np.diff(np.concatenate([angs, [angs[0] + 2 * math.pi]]))
    imax = int(np.argmax(gaps))
    start = angs[(imax + 1) % len(angs)]
    span = 2 * math.pi - gaps[imax]
    midr = (r0 + maxr) / 2
    dtheta = max(0.10, (2 * lot_depth + reserve) / max(midr, 1.0))  # arc ~ block width
    nspokes = max(2, int(span / dtheta) + 1)
    for k in range(nspokes + 1):
        th = start + span * k / nspokes
        near = (cx + r0 * 0.6 * math.cos(th), cy + r0 * 0.6 * math.sin(th))
        far = (cx + (maxr + spacing) * math.cos(th), cy + (maxr + spacing) * math.sin(th))
        seg = LineString([near, far]).intersection(inset)
        lines.extend([l for l in _lines(seg) if l.length > MIN_ROAD_PIECE])
    if not lines:
        raise ValueError("Could not fit a radiant network in the parcel.")

    lines = _connect_components(lines, parcel, reserve)
    grid_union = unary_union(lines)
    for ap in access_m:
        npt = nearest_points(grid_union, ap)[0]
        if npt.distance(ap) > 1.0:
            lines.append(LineString([(ap.x, ap.y), (npt.x, npt.y)]))
    return lines


def _connect_components(lines, parcel, reserve, max_join=400.0):
    """Concave parcels fragment the grid into disconnected pieces; join nearby
    components with straight connectors that stay inside the parcel."""
    corridor = parcel.buffer(-2.0)
    for _ in range(12):
        u = unary_union(lines)
        comps = _lines(u)
        # group noded lines into connected components via shared endpoints
        parent = list(range(len(comps)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(comps)):
            for j in range(i + 1, len(comps)):
                if comps[i].distance(comps[j]) < 0.05:
                    pi, pj = find(i), find(j)
                    if pi != pj:
                        parent[pi] = pj
        groups = {}
        for i in range(len(comps)):
            groups.setdefault(find(i), []).append(comps[i])
        if len(groups) <= 1:
            break
        glist = [unary_union(g) for g in groups.values()]
        glist.sort(key=lambda g: -g.length)
        # connect the largest group to its nearest neighbour group
        base = glist[0]
        best = None
        for other in glist[1:]:
            p1, p2 = nearest_points(base, other)
            conn = LineString([p1, p2])
            if conn.length < max_join and corridor.contains(conn):
                if best is None or conn.length < best.length:
                    best = conn
        if best is None:
            # drop unreachable fragments (pruning would discard them anyway)
            lines = _lines(base)
            break
        lines = _lines(unary_union(lines)) + [best]
    return lines


# --------------------------------------------------------------- network pruning
def prune_network(lines, access_m, parcel):
    """Keep segments reachable from access points; trim short dead stubs.
    Returns (segments, deadend_nodes)."""
    if not lines:
        return [], []
    segs = _segments(unary_union(lines))
    if not segs:
        return [], []

    def build_adj(segs):
        adj = {}
        for i, (a, b) in enumerate(segs):
            na, nb = _node(a), _node(b)
            adj.setdefault(na, []).append((i, nb))
            adj.setdefault(nb, []).append((i, na))
        return adj

    adj = build_adj(segs)
    nodes = list(adj.keys())
    starts = set()
    for ap in access_m:
        starts.add(min(nodes, key=lambda n: (n[0] - ap.x) ** 2 + (n[1] - ap.y) ** 2))
    if not starts and nodes:
        starts.add(nodes[0])

    seen, seen_segs, queue = set(starts), set(), list(starts)
    while queue:
        n = queue.pop()
        for si, other in adj.get(n, []):
            seen_segs.add(si)
            if other not in seen:
                seen.add(other)
                queue.append(other)
    segs = [s for i, s in enumerate(segs) if i in seen_segs]

    access_nodes = {_node((p.x, p.y)) for p in access_m}
    boundary = parcel.exterior
    # trim short dead-end CHAINS (walk from each degree-1 node to the next junction;
    # drop the whole chain if it is shorter than a viable cul-de-sac)
    for _ in range(10):
        adj = build_adj(segs)
        deg = {n: len(v) for n, v in adj.items()}
        drop = set()
        for n, d in deg.items():
            if d != 1 or n in access_nodes or boundary.distance(Point(n)) < 2.0:
                continue
            chain_len, chain_segs, cur, prev_seg = 0.0, [], n, None
            ok = True
            while True:
                nxt = [(si, o) for si, o in adj.get(cur, []) if si != prev_seg]
                if not nxt:
                    ok = False
                    break
                si, other = nxt[0]
                a, b = segs[si]
                chain_len += math.hypot(b[0] - a[0], b[1] - a[1])
                chain_segs.append(si)
                if deg.get(other, 0) != 2 or chain_len > MIN_CULDESAC_LEN:
                    break
                cur, prev_seg = other, si
            if ok and chain_len < MIN_CULDESAC_LEN:
                drop.update(chain_segs)
        if not drop:
            break
        segs = [s for i, s in enumerate(segs) if i not in drop]

    adj = build_adj(segs)
    deadends = [n for n, v in adj.items()
                if len(v) == 1 and n not in access_nodes
                and boundary.distance(Point(n)) > CULDESAC_HEAD_RESERVE_RADIUS]
    return segs, deadends


def chains(segs):
    """Merge noded segments into chains between junctions (degree != 2 nodes)."""
    if not segs:
        return []
    m = linemerge(MultiLineString([LineString([a, b]) for a, b in segs]))
    return _lines(m)


def connectivity_index(segs):
    nodes = set()
    for a, b in segs:
        nodes.add(_node(a))
        nodes.add(_node(b))
    ch = chains(segs)
    return round(len(ch) / max(1, len([n for n in nodes
                                       if sum(1 for a, b in segs if _node(a) == n or _node(b) == n) != 2])), 2)


# --------------------------------------------------------------- warping
def _resample(coords, step):
    ln = LineString(coords)
    n = max(2, int(ln.length / step) + 1)
    return [ln.interpolate(i / (n - 1), normalized=True).coords[0] for i in range(n)]


def _smooth_interior(pts, passes=SMOOTH_PASSES):
    pts = [list(p) for p in pts]
    for _ in range(passes):
        for i in range(1, len(pts) - 1):
            pts[i][0] = 0.25 * pts[i - 1][0] + 0.5 * pts[i][0] + 0.25 * pts[i + 1][0]
            pts[i][1] = 0.25 * pts[i - 1][1] + 0.5 * pts[i][1] + 0.25 * pts[i + 1][1]
    return [tuple(p) for p in pts]


def warp_chains(chain_list, parcel, terrain, max_dev):
    """Nudge chain interiors to follow contours (terrain) or add gentle curvature (flat).
    Chain endpoints (junctions) are fixed; result stays inside the buffered parcel."""
    safe = parcel.buffer(-2.0)
    out = []
    for ch in chain_list:
        if ch.length < 3 * WARP_SAMPLE:
            out.append(ch)
            continue
        pts = _resample(list(ch.coords), WARP_SAMPLE)
        n = len(pts)
        new = [pts[0]]
        if terrain is not None:
            z0 = terrain.elev(*pts[0])
            z1 = terrain.elev(*pts[-1])
            for i in range(1, n - 1):
                x, y = pts[i]
                # local unit normal
                (xa, ya), (xb, yb) = pts[i - 1], pts[i + 1]
                tx, ty = xb - xa, yb - ya
                tl = math.hypot(tx, ty) or 1.0
                nx, ny = -ty / tl, tx / tl
                z_target = z0 + (z1 - z0) * (i / (n - 1))
                best_t, best_cost = 0.0, abs(terrain.elev(x, y) - z_target)
                for t in np.linspace(-max_dev, max_dev, 9):
                    cand = (x + nx * t, y + ny * t)
                    if not safe.contains(Point(cand)):
                        continue
                    cost = abs(terrain.elev(*cand) - z_target) + 0.012 * abs(t)
                    if cost < best_cost:
                        best_cost, best_t = cost, t
                new.append((x + nx * best_t, y + ny * best_t))
        else:
            amp = min(FLAT_CURVE_AMP, ch.length / 60.0)
            wavelength = max(120.0, ch.length / 2.2)
            for i in range(1, n - 1):
                x, y = pts[i]
                (xa, ya), (xb, yb) = pts[i - 1], pts[i + 1]
                tx, ty = xb - xa, yb - ya
                tl = math.hypot(tx, ty) or 1.0
                nx, ny = -ty / tl, tx / tl
                s = i * WARP_SAMPLE
                t = amp * math.sin(2 * math.pi * s / wavelength) * math.sin(math.pi * i / (n - 1))
                cand = (x + nx * t, y + ny * t)
                new.append(cand if safe.contains(Point(cand)) else (x, y))
        new.append(pts[-1])
        out.append(LineString(_smooth_interior(new)))
    return out
