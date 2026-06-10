"""End-to-end smoke test: fetch a real parcel from LIST, generate a layout,
export PDF + shapefile. Run: python test_generate.py [PID]"""
import json
import sys

import requests

from exporters.pdf_export import export_pdf
from exporters.shapefile_export import export_zip
from generator.layout import generate
from generator.params import GenParams

PARCEL_LAYER = "https://services.thelist.tas.gov.au/arcgis/rest/services/Public/CadastreParcels/MapServer/0/query"


def find_test_parcel():
    """Find a ~3-8 ha private parcel near Brighton (growth area) to exercise the generator."""
    r = requests.get(PARCEL_LAYER, params={
        "where": "COMP_AREA > 30000 AND COMP_AREA < 80000 AND CAD_TYPE1 = 'Private Parcel'",
        "geometry": "147.23,-42.70,147.32,-42.64", "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
        "outFields": "PID,PROP_ADD,COMP_AREA", "returnGeometry": "true", "outSR": "4326",
        "resultRecordCount": 5, "f": "geojson",
    }, timeout=30)
    feats = r.json()["features"]
    assert feats, "no test parcel found"
    return feats[0]


def main():
    if len(sys.argv) > 1:
        from services.list_api import get_parcel_by_pid
        feat = get_parcel_by_pid(int(sys.argv[1]))
    else:
        feat = find_test_parcel()
    props = feat["properties"]
    print(f"Parcel: {props.get('PROP_ADD')} | PID {props.get('PID')} | {props.get('COMP_AREA', 0):,.0f} m2")

    params = GenParams.from_dict({"zone": "GRZ", "target_lot_area": 600})
    result = generate(feat["geometry"], params)

    s = result["stats"]
    print(f"Lots: {s['lot_count']} ({s['compliant_lots']} compliant) | "
          f"{s['min_lot_area']:.0f}-{s['max_lot_area']:.0f} m2, avg {s['avg_lot_area']:.0f} | "
          f"road {s['total_road_length_m']:.0f} m ({s['road_area_pct']}%) | "
          f"yield {s['yield_per_ha']}/ha | angle {s['road_angle_deg']}")
    for w in result["warnings"]:
        print("  WARN:", w.encode("ascii", "replace").decode())

    pdf = export_pdf(result, s, {"address": props.get("PROP_ADD", ""), "pid": props.get("PID")})
    with open("test_output_plan.pdf", "wb") as f:
        f.write(pdf)
    shp = export_zip(result)
    with open("test_output_shp.zip", "wb") as f:
        f.write(shp)
    with open("test_output_layers.json", "w") as f:
        json.dump(result, f)
    print("Wrote test_output_plan.pdf, test_output_shp.zip, test_output_layers.json")

    # sanity assertions
    assert s["lot_count"] >= 10, "expected a reasonable lot yield"
    assert s["compliant_lots"] / s["lot_count"] > 0.5, "most lots should be compliant"
    assert result["roads"]["features"], "roads missing"
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
