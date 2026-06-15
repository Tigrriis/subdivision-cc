"""Lot subdivision of blocks into regular rectangular lots.

Method (avoids the thin/irregular faces the old medial-axis polygonise produced):
  1. Find frontage chains (block boundary shared with the road reserve).
  2. Process chains longest-first. For each, build a *lot row* = a single-sided
     strip of depth `lot_depth` measured perpendicular from the frontage, clipped
     to the block area not yet claimed by an earlier row. The strip's front and
     rear edges are parallel, so its cross-section is rectangular.
  3. Split the row into uniform-width pieces with cuts perpendicular to the
     frontage (uniform spacing => no slivers). Straight frontages give rectangles;
     curved/corner frontages give chamfered/trapezoidal lots, as intended.
  4. Whatever the rows don't claim (deep block cores) becomes balance / open space.
"""
import math

from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import split, unary_union

FRONTAGE_TOL = 0.6       # m: boundary within this of reserve counts as frontage
DENSIFY = 2.0            # m: boundary sampling step
MIN_LOT_DIM = 7.0        # m: discard/merge pieces thinner than this
ELONG_MAX = 5.0          # max length:width before a piece is treated as a sliver


def _polys(geom):
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if hasattr(geom, "geoms"):
        out = []
        for g in geom.geoms:
            out.extend(_polys(g))
        return out
    return []


def _largest(geom):
    ps = _polys(geom)
    return max(ps, key=lambda g: g.area) if ps else None


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
    if len(chains) >= 2 and near[0] and near[-1]:  # join wrap-around
        first, last = chains[0], chains[-1]
        chains = chains[1:-1] + [LineString(list(last.coords) + list(first.coords))]
    return [c for c in chains if c.length > 6.0]


def _strip(chain, region, depth):
    """Single-sided strip of `depth` from the chain, on whichever side lies inside region."""
    best, best_area = None, 0.0
    for d in (depth, -depth):
        try:
            s = chain.buffer(d, single_sided=True).buffer(0)
        except Exception:
            continue
        inter = s.intersection(region)
        if inter.area > best_area:
            best_area, best = inter.area, inter
    return _largest(best)


def _elongation(poly):
    mrr = poly.minimum_rotated_rectangle
    cs = list(mrr.exterior.coords)
    d1 = math.hypot(cs[1][0] - cs[0][0], cs[1][1] - cs[0][1])
    d2 = math.hypot(cs[2][0] - cs[1][0], cs[2][1] - cs[1][1])
    short, long = min(d1, d2), max(d1, d2)
    return (long / short if short > 0.1 else 99.0), short


def _split_row(row, chain, target_w):
    """Cut a lot row into uniform-width pieces, perpendicular to the frontage."""
    L = chain.length
    n = max(1, round(L / target_w))
    aw = L / n
    pieces = [row]
    for k in range(1, n):
        s = k * aw
        p = chain.interpolate(s)
        a = chain.interpolate(max(0.0, s - 3.0))
        b = chain.interpolate(min(L, s + 3.0))
        tx, ty = b.x - a.x, b.y - a.y
        tl = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / tl, tx / tl
        # cut spans well past the row both ways so split() fully severs the piece
        cut = LineString([(p.x - nx * 400, p.y - ny * 400), (p.x + nx * 400, p.y + ny * 400)])
        newp = []
        for pc in pieces:
            if cut.intersects(pc):
                newp.extend(_polys(split(pc, cut)))
            else:
                newp.append(pc)
        pieces = newp
    return pieces


def subdivide_block(block, reserve_edge, lot_depth, target_area, min_area, min_frontage, rng=None):
    """Returns (lots, open_space). Each lot is (polygon, frontage_len)."""
    block = _largest(block.buffer(0))
    if block is None:
        return [], []
    chains = _frontage_chains(block, reserve_edge)
    if not chains:
        return [], [block]

    target_w = max(min_frontage, target_area / lot_depth)

    def frontage_len(poly):
        return poly.exterior.intersection(reserve_edge.buffer(FRONTAGE_TOL)).length

    # build lot rows longest frontage first; subtract claimed area so opposite
    # rows in a shallow (double-loaded) block meet cleanly instead of overlapping
    remaining = block
    rows = []
    for chain in sorted(chains, key=lambda c: -c.length):
        row = _strip(chain, remaining, lot_depth)
        if row is None or row.area < min_area * 0.5:
            continue
        rows.append((row, chain))
        remaining = _largest(remaining.difference(row.buffer(-0.05))) or remaining

    lots = []
    for row, chain in rows:
        for pc in _split_row(row, chain, target_w):
            if pc.area < 1.0:
                continue
            lots.append({"g": pc, "f": frontage_len(pc)})

    # merge sub-minimum / slivered end pieces into the neighbour they share the
    # most frontage-parallel edge with (keeps lots rectangular, removes slivers)
    def merge_pass():
        for i, li in enumerate(lots):
            elong, short = _elongation(li["g"])
            too_small = li["g"].area < min_area
            too_thin = short < MIN_LOT_DIM or elong > ELONG_MAX
            too_narrow = 1.0 < li["f"] < min_frontage * 0.7
            if not (too_small or too_thin or too_narrow):
                continue
            best_j, best_share = None, 0.0
            for j, lj in enumerate(lots):
                if j == i:
                    continue
                share = li["g"].buffer(0.05).intersection(lj["g"].buffer(0.05)).area
                if share > best_share:
                    best_share, best_j = share, j
            if best_j is None:
                continue
            u = _largest(unary_union([li["g"], lots[best_j]["g"]]))
            if u is None:
                continue
            lots[best_j]["g"] = u
            lots[best_j]["f"] = frontage_len(u)
            lots.pop(i)
            return True
        return False

    guard = 0
    while guard < 300 and merge_pass():
        guard += 1

    out_lots, open_space = [], []
    leftover = _polys(remaining) if isinstance(remaining, (Polygon, MultiPolygon)) else []
    for li in lots:
        elong, short = _elongation(li["g"])
        if li["f"] < 1.0:
            open_space.append(li["g"])          # landlocked
        elif li["g"].area < min_area * 0.55 or short < MIN_LOT_DIM or elong > ELONG_MAX:
            open_space.append(li["g"])          # unbuildable sliver — not a real lot
        elif li["g"].area > 4.0 * target_area:
            open_space.append(li["g"])          # under-served residue
        else:
            out_lots.append((li["g"], li["f"]))
    for g in leftover:
        if g.area > min_area * 0.4:
            open_space.append(g)                # deep block core / balance land
    return out_lots, open_space


# Backwards-compatible name used by layout.py (no recursion needed with row method).
def subdivide_block_recursive(block, reserve_edge, lot_depth, target_area, min_area,
                              min_frontage, rng=None, depth=0):
    return subdivide_block(block, reserve_edge, lot_depth, target_area, min_area,
                           min_frontage, rng)
