"""Generative Subdivision Layout App — Flask backend.

Run locally:  python app.py   (http://127.0.0.1:5000)
PythonAnywhere: point the WSGI config at `app` in this module.
"""
import io
import zipfile

import shapefile as pyshp
from flask import Flask, jsonify, render_template, request, send_file
from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union

from exporters.pdf_export import export_pdf
from exporters.shapefile_export import export_zip
from generator.layout import generate
from generator.params import GenParams, ROAD_PRESETS, ZONES
from services import elevation, list_api

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024


@app.get("/")
def index():
    return render_template(
        "index.html",
        zones={k: {"name": v.name, "min_lot_area": v.min_lot_area, "min_frontage": v.min_frontage,
                   "default_lot_depth": v.default_lot_depth} for k, v in ZONES.items()},
        road_presets={k: v["label"] for k, v in ROAD_PRESETS.items()},
    )


# ---------------------------------------------------------------- LIST lookups
@app.get("/api/search")
def api_search():
    q = request.args.get("q", "")
    try:
        return jsonify({"results": list_api.search_address(q)})
    except list_api.ListApiError as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/parcel")
def api_parcel():
    try:
        if request.args.get("pid"):
            feat = list_api.get_parcel_by_pid(int(request.args["pid"]))
        else:
            feat = list_api.get_parcel_at_point(float(request.args["lon"]), float(request.args["lat"]))
        return jsonify({"feature": feat})
    except list_api.ListApiError as e:
        return jsonify({"error": str(e)}), 404
    except (TypeError, ValueError):
        return jsonify({"error": "Provide ?pid= or ?lon=&lat="}), 400


# ---------------------------------------------------------------- boundary upload
@app.post("/api/upload_boundary")
def api_upload_boundary():
    """Accepts a zipped shapefile; returns the (dissolved) boundary as GeoJSON WGS84."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        zf = zipfile.ZipFile(io.BytesIO(f.read()))
        names = {n.lower().rsplit(".", 1)[-1]: n for n in zf.namelist() if "." in n}
        if "shp" not in names or "dbf" not in names:
            return jsonify({"error": "Zip must contain .shp and .dbf (and ideally .prj)"}), 400
        r = pyshp.Reader(
            shp=io.BytesIO(zf.read(names["shp"])),
            dbf=io.BytesIO(zf.read(names["dbf"])),
            shx=io.BytesIO(zf.read(names["shx"])) if "shx" in names else None,
        )
        geoms = [shape(s.__geo_interface__) for s in r.shapes() if s.shapeType in (5, 15, 25)]
        if not geoms:
            return jsonify({"error": "No polygon features in shapefile"}), 400
        merged = unary_union([g.buffer(0) for g in geoms])

        # CRS: use .prj if present, else assume GDA94/MGA55
        src = CRS.from_epsg(28355)
        assumed = True
        if "prj" in names:
            try:
                src = CRS.from_wkt(zf.read(names["prj"]).decode("utf-8", "ignore"))
                assumed = False
            except Exception:
                pass
        if not src.equals(CRS.from_epsg(4326)):
            t = Transformer.from_crs(src, 4326, always_xy=True)
            merged = transform(t.transform, merged)
        feat = {"type": "Feature", "geometry": mapping(merged),
                "properties": {"PID": None, "PROP_ADD": f.filename,
                               "source": "upload", "crs_assumed": assumed}}
        return jsonify({"feature": feat})
    except zipfile.BadZipFile:
        return jsonify({"error": "Upload a .zip containing the shapefile parts"}), 400
    except Exception as e:
        return jsonify({"error": f"Could not read shapefile: {e}"}), 400


# ---------------------------------------------------------------- generation
@app.post("/api/generate")
def api_generate():
    d = request.get_json(force=True)
    geom = d.get("parcel_geometries") or d.get("parcel_geometry")
    if not geom:
        return jsonify({"error": "parcel_geometry or parcel_geometries required"}), 400
    try:
        params = GenParams.from_dict(d.get("params", {}))
        result = generate(geom, params, roads_override=d.get("roads_override"))
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        app.logger.exception("generation failed")
        return jsonify({"error": f"Generation failed: {e}"}), 500


@app.post("/api/grades")
def api_grades():
    d = request.get_json(force=True)
    roads = d.get("roads")
    if not roads or not roads.get("features"):
        return jsonify({"error": "roads FeatureCollection required"}), 400
    try:
        return jsonify(elevation.check_road_grades(roads))
    except Exception as e:
        return jsonify({"available": False, "error": f"Elevation service unavailable: {e}"}), 502


# ---------------------------------------------------------------- exports
@app.post("/api/export/shp")
def api_export_shp():
    layers = request.get_json(force=True)
    data = export_zip(layers)
    return send_file(io.BytesIO(data), mimetype="application/zip",
                     as_attachment=True, download_name="subdivision_layout.zip")


@app.post("/api/export/pdf")
def api_export_pdf():
    d = request.get_json(force=True)
    zone = d.get("stats", {}).get("zone", "")
    meta = d.get("meta", {})
    if zone in ZONES:
        meta["zone_name"] = ZONES[zone].name
    data = export_pdf(d.get("layers", {}), d.get("stats", {}), meta)
    return send_file(io.BytesIO(data), mimetype="application/pdf",
                     as_attachment=True, download_name="subdivision_concept_plan.pdf")


if __name__ == "__main__":
    import os
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)), use_reloader=False)
