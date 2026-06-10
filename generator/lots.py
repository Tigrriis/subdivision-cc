"""Lot subdivision of blocks with curved frontages.

For each block: find frontage chains (boundary shared with road reserve), place
cut lines perpendicular to the *local* frontage tangent at jittered spacing,
add a spine cut equidistant from frontages, polygonise, then merge undersized
or landlocked faces. Lot orientation therefore follows the street, including
around curves and corners.
"""
import math

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import polygonize, unary_union

FRONTAGE_TOL = 0.35      # m: boundary within this of reserve counts as frontage
DENSIFY = 2.0            # m: boundary sampling step
CUT_OVERSHOOT = 1.75     # cut length = lot_depth * this
JITTER = 0.10            # +/- proportion of frontage width


def _polys(geom):
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if hasattr(geom, "geoms"):
        out = []
        for g in geom.geoms:
            out.extend(_polys(g))
        return out
    return []


def _frontage_chains(block, reserve_edge):
    """Continuous runs of the block boundary that touch the road reserve."""
    ext = block.exterior
    n = max(8, int(ext.length / DENSIFY))
    pts = [ext.interpolate(i / n, normalized=True) for i in range(n + 1)]
    near = [reserve_edge.distance(p) < FRONTAGE_TOL for p in pts]
    chains, cur = [], []
    for p, isnear in zip(pts, near):
        if isnear:
            cur.append((p.x, p.y))
        else:
            if len(cur) >= 3:
                chains.append(LineString(cur))
            cur = []
    if len(cur) >= 3:
        chains.append(LineString(cur))
    # join wrap-around (start/end of ring both frontage)
    if len(chains) >= 2 and near[0] and near[-1]:
        first, last = chains[0], chains[-1]
        chains = chains[1:-1] + [LineString(list(last.coords) + list(first.coords))]
    return [c for c in chains if c.length > 6.0]


def _cuts_for_chain(chain, block, w, cut_depth, rng):
    """Perpendicular cut lines along a frontage chain at jittered spacing."""
    cuts = []
    L = chain.length
    if L < w * 1.35:
        return cuts
    pos = w * (1 + rng.uniform(-JITTER, JITTER))
    while pos < L - w * 0.55:
        p = chain.interpolate(pos)
        a = chain.interpolate(max(0.0, pos - 3.0))
        b = chain.interpolate(min(L, pos + 3.0))
        tx, ty = b.x - a.x, b.y - a.y
        tl = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / tl, tx / tl
        # ensure normal points into the block
        probe = Point(p.x + nx * 2.0, p.y + ny * 2.0)
        if not block.contains(probe):
            nx, ny = -nx, -ny
        cuts.append(LineString([(p.x - nx * 0.5, p.y - ny * 0.5),
                                (p.x + nx * cut_depth, p.y + ny * cut_depth)]))
        pos += w * (1 + rng.uniform(-JITTER, JITTER))
    return cuts


def subdivide_block(block, reserve_edge, lot_depth, target_area, min_area, min_frontage, rng):
    """Returns (lot_polys, open_space_polys). Each lot is (polygon, frontage_len)."""
    w = max(10.0, target_area / lot_depth)
    chains = _frontage_chains(block, reserve_edge)
    if not chains:
        return [], [block]

    # spine: equidistant ridge between opposite frontages. For double-loaded
    # blocks (~2 x lot_depth deep) buffer(-lot_depth) vanishes, so back off the
    # erosion until a ridge appears (it sits on the block midline).
    e = float(lot_depth)
    spine = block.buffer(-e)
    while spine.is_empty and e > 6.0:
        e -= 3.0
        spine = block.buffer(-e)

    cut_depth = max(lot_depth * CUT_OVERSHOOT, e * 1.5 + 8.0)
    cuts = []
    for ch in chains:
        cuts.extend(_cuts_for_chain(ch, block, w, cut_depth, rng))

    edges = [block.exterior] + [LineString(r.coords) for r in block.interiors]
    for c in cuts:
        edges.append(c)
    if not spine.is_empty:
        for sp in _polys(spine):
            edges.append(LineString(sp.exterior.coords))

    faces = [f for f in polygonize(unary_union(edges))
             if f.representative_point().within(block)]
    if not faces:
        return [(block, block.exterior.intersection(reserve_edge.buffer(FRONTAGE_TOL)).length)], []

    # classify and merge
    def frontage_len(poly):
        return poly.exterior.intersection(reserve_edge.buffer(FRONTAGE_TOL)).length

    lots = [{"g": f, "f": frontage_len(f)} for f in faces]

    def merge_into_neighbour(i):
        """Merge lots[i] into the adjacent lot sharing the longest boundary."""
        best_j, best_len = None, 0.0
        gi = lots[i]["g"]
        for j, lj in enumerate(lots):
            if j == i:
                continue
            shared = gi.buffer(0.05).intersection(lj["g"].buffer(0.05)).area
            if shared > best_len:
                best_len, best_j = shared, j
        if best_j is None:
            return False
        u = unary_union([gi, lots[best_j]["g"]])
        parts = _polys(u)
        if len(parts) != 1:
            return False
        lots[best_j]["g"] = parts[0]
        lots[best_j]["f"] = frontage_len(parts[0])
        lots.pop(i)
        return True

    # iterate: undersized / narrow faces and SMALL landlocked slivers merge into
    # neighbours; LARGE landlocked back-land must not inflate a lot — it becomes
    # open space / residue instead.
    changed = True
    guard = 0
    while changed and guard < 200:
        changed = False
        guard += 1
        for i in range(len(lots)):
            li = lots[i]
            landlocked_small = li["f"] < 1.0 and li["g"].area < 2.0 * min_area
            undersized = li["f"] >= 1.0 and li["g"].area < min_area
            narrow = li["f"] > 1.0 and li["f"] < min_frontage * 0.7
            if landlocked_small or undersized or narrow:
                if merge_into_neighbour(i):
                    changed = True
                    break

    out_lots, open_space = [], []
    for li in lots:
        if li["f"] < 1.0:
            open_space.append(li["g"])  # landlocked back-land
        elif li["f"] < min_frontage * 1.2 and li["g"].area > 2.5 * target_area:
            open_space.append(li["g"])  # balance land with token frontage — not a real lot
        elif li["g"].area > 4.0 * target_area:
            open_space.append(li["g"])  # under-served residue — extend a road to serve it
        else:
            out_lots.append((li["g"], li["f"]))
    return out_lots, open_space


def subdivide_block_recursive(block, reserve_edge, lot_depth, target_area, min_area,
                              min_frontage, rng, depth=0):
    """subdivide_block + re-subdivision of oversized faces (deep blocks produce
    wrap-around residue faces that keep a sliver of frontage; treating them as new
    blocks slices them properly and sends their landlocked core to open space)."""
    lots, open_space = subdivide_block(block, reserve_edge, lot_depth, target_area,
                                       min_area, min_frontage, rng)
    if depth >= 2:
        return lots, open_space
    out = []
    for g, f in lots:
        if g.area > 2.6 * target_area:
            sub_lots, sub_os = subdivide_block(g, reserve_edge, lot_depth, target_area,
                                               min_area, min_frontage, rng)
            if len(sub_lots) > 1:
                deeper, deeper_os = [], list(sub_os)
                for sg, sf in sub_lots:
                    if sg.area > 2.6 * target_area and depth + 1 < 2:
                        dl, dos = subdivide_block_recursive(
                            sg, reserve_edge, lot_depth, target_area, min_area,
                            min_frontage, rng, depth + 2)
                        deeper.extend(dl)
                        deeper_os.extend(dos)
                    else:
                        deeper.append((sg, sf))
                out.extend(deeper)
                open_space.extend(deeper_os)
                continue
        out.append((g, f))
    return out, open_space
