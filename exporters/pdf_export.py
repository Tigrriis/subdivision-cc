"""A3 landscape PDF concept plan with scale bar and north arrow (matplotlib)."""
import datetime
import io
import math

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon, FancyArrow
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

_TO_MGA = Transformer.from_crs(4326, 28355, always_xy=True)

NICE_SCALES = [200, 250, 500, 1000, 1250, 2000, 2500, 5000, 10000]
A3 = (16.54, 11.69)  # inches
MAP_W_IN, MAP_H_IN = 13.2, 10.2  # drawable map panel


def _geoms_m(fc):
    return [transform(_TO_MGA.transform, shape(f["geometry"])) for f in fc.get("features", [])]


def _draw_poly(ax, poly, **kw):
    if poly.geom_type == "MultiPolygon":
        for g in poly.geoms:
            _draw_poly(ax, g, **kw)
        return
    ax.add_patch(MplPolygon(list(poly.exterior.coords), closed=True, **kw))
    for hole in poly.interiors:
        ax.add_patch(MplPolygon(list(hole.coords), closed=True, facecolor="white", edgecolor="none"))


def export_pdf(layers: dict, stats: dict, meta: dict | None = None) -> bytes:
    meta = meta or {}
    parcel = _geoms_m(layers.get("parcel", {}))
    lots = layers.get("lots", {}).get("features", [])
    lots_m = _geoms_m(layers.get("lots", {}))
    reserve = _geoms_m(layers.get("reserve", {}))
    carriageway = _geoms_m(layers.get("carriageway", {}))
    roads = _geoms_m(layers.get("roads", {}))
    open_space = _geoms_m(layers.get("open_space", {}))

    if not parcel:
        raise ValueError("No parcel to plot")
    minx, miny, maxx, maxy = parcel[0].bounds
    for g in lots_m + reserve:
        b = g.bounds
        minx, miny = min(minx, b[0]), min(miny, b[1])
        maxx, maxy = max(maxx, b[2]), max(maxy, b[3])
    w_m, h_m = maxx - minx, maxy - miny

    # pick a nice round scale that fits the map panel
    req = max(w_m / (MAP_W_IN * 0.0254), h_m / (MAP_H_IN * 0.0254))
    scale = next((s for s in NICE_SCALES if s >= req * 1.05), NICE_SCALES[-1])
    half_w, half_h = MAP_W_IN * 0.0254 * scale / 2, MAP_H_IN * 0.0254 * scale / 2
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2

    fig = plt.figure(figsize=A3)
    ax = fig.add_axes([0.02, 0.03, MAP_W_IN / A3[0], MAP_H_IN / A3[1]])
    ax.set_xlim(cx - half_w, cx + half_w)
    ax.set_ylim(cy - half_h, cy + half_h)
    ax.set_aspect("equal")
    ax.axis("off")

    for g in open_space:
        _draw_poly(ax, g, facecolor="#d9ead3", edgecolor="#6aa84f", linewidth=0.6)
    for g in reserve:
        _draw_poly(ax, g, facecolor="#eeeeee", edgecolor="#999999", linewidth=0.5)
    for g in carriageway:
        _draw_poly(ax, g, facecolor="#cccccc", edgecolor="none")
    for g in roads:
        gs = g.geoms if g.geom_type == "MultiLineString" else [g]
        for ln in gs:
            xs, ys = zip(*ln.coords)
            ax.plot(xs, ys, color="white", linewidth=0.9, linestyle=(0, (6, 3)))
    for feat, g in zip(lots, lots_m):
        ok = feat["properties"].get("compliant", True)
        _draw_poly(ax, g, facecolor="#fff6d5" if ok else "#fde0dc",
                   edgecolor="#b8860b" if ok else "#cc4125", linewidth=0.7)
        c = g.representative_point()
        if g.area > 80:
            ax.text(c.x, c.y, f"{feat['properties']['id']}\n{feat['properties']['area_m2']:.0f}m²",
                    fontsize=5 if scale > 1500 else 6.5, ha="center", va="center", color="#444444")
    for g in parcel:
        xs, ys = zip(*g.exterior.coords)
        ax.plot(xs, ys, color="#1155cc", linewidth=1.6)

    # ---- title block ----
    fig.text(0.81, 0.96, "SUBDIVISION CONCEPT PLAN", fontsize=12.5, fontweight="bold")
    addr = meta.get("address", "")
    lines = [
        addr,
        f"PID: {meta.get('pid', '—')}",
        f"Zone: {stats.get('zone', '')} — {meta.get('zone_name', '')}",
        "",
        f"Parcel area: {stats.get('parcel_area_ha', 0):.2f} ha",
        f"Lots: {stats.get('lot_count', 0)}  (compliant: {stats.get('compliant_lots', 0)})",
        f"Lot size: {stats.get('min_lot_area', 0):.0f}–{stats.get('max_lot_area', 0):.0f} m² "
        f"(avg {stats.get('avg_lot_area', 0):.0f} m²)",
        f"Yield: {stats.get('yield_per_ha', 0)} lots/ha",
        f"New road: {stats.get('total_road_length_m', 0):.0f} m "
        f"({stats.get('road_area_pct', 0)}% of site)",
        "",
        f"Scale 1:{scale} (A3)",
        f"CRS: GDA94 / MGA55   Date: {datetime.date.today().isoformat()}",
        "",
        "CONCEPT ONLY — subject to survey,",
        "council & TasWater approval.",
        "Cadastre: © State of Tasmania (the LIST)",
    ]
    fig.text(0.81, 0.93, "\n".join(lines), fontsize=8, va="top", linespacing=1.55)

    # ---- scale bar (bottom right of map): ~5 cm of paper, nice round metres ----
    nice = [10, 20, 25, 50, 100, 150, 200, 250, 500, 1000]
    bar_m = next((n for n in nice if n >= scale * 0.05), nice[-1])
    bx0 = cx + half_w - bar_m - scale * 0.02
    by = cy - half_h + scale * 0.015
    seg = bar_m / 4
    for i in range(4):
        ax.add_patch(MplPolygon([(bx0 + i * seg, by), (bx0 + (i + 1) * seg, by),
                                 (bx0 + (i + 1) * seg, by + scale * 0.004), (bx0 + i * seg, by + scale * 0.004)],
                                closed=True, facecolor="black" if i % 2 == 0 else "white",
                                edgecolor="black", linewidth=0.6))
    for i in (0, 2, 4):
        ax.text(bx0 + i * seg, by + scale * 0.006, f"{i * seg:.0f}",
                fontsize=6, ha="center", va="bottom")
    ax.text(bx0 + bar_m / 2, by - scale * 0.004, "metres", fontsize=6, ha="center", va="top")

    # ---- north arrow (top left of map) ----
    nx, ny = cx - half_w + scale * 0.02, cy + half_h - scale * 0.05
    ax.add_patch(FancyArrow(nx, ny, 0, scale * 0.03, width=scale * 0.004,
                            head_width=scale * 0.012, head_length=scale * 0.012,
                            facecolor="black", edgecolor="black"))
    ax.text(nx, ny + scale * 0.048, "N", fontsize=11, fontweight="bold", ha="center")

    out = io.BytesIO()
    fig.savefig(out, format="pdf")
    plt.close(fig)
    return out.getvalue()
