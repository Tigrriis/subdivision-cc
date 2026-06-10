"""LIST Tasmania (thelist.tas.gov.au) ArcGIS REST integration.

Public services, no API key. Endpoints verified 2026-06-10:
- Address search: Public/SearchService/MapServer/7 (Address Geocodes; fields ADDRESS, PID)
- Parcels:        Public/CadastreParcels/MapServer/0 (fields PID, PROP_ADD, COMP_AREA, MEAS_AREA)

Data: (c) State of Tasmania — attribution required (Land Tasmania).
"""
import requests

BASE = "https://services.thelist.tas.gov.au/arcgis/rest/services/Public"
SEARCH_LAYER = f"{BASE}/SearchService/MapServer/7/query"
PARCEL_LAYER = f"{BASE}/CadastreParcels/MapServer/0/query"
TIMEOUT = 30


class ListApiError(Exception):
    pass


def _get(url, params):
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
    except requests.RequestException as e:
        raise ListApiError(f"LIST service unreachable: {e}") from e
    if isinstance(d, dict) and d.get("error"):
        raise ListApiError(f"LIST service error: {d['error'].get('message', d['error'])}")
    return d


def search_address(q: str, limit: int = 8) -> list[dict]:
    """Free-text address search -> [{address, pid}]."""
    q = q.strip().upper().replace("'", "''")
    if not q:
        return []
    where = " AND ".join(f"ADDRESS LIKE '%{tok}%'" for tok in q.split()[:6])
    d = _get(SEARCH_LAYER, {
        "where": where, "outFields": "ADDRESS,PID", "returnGeometry": "false",
        "resultRecordCount": limit, "f": "json",
    })
    out, seen = [], set()
    for f in d.get("features", []):
        a = f.get("attributes", {})
        pid = a.get("PID")
        if pid is None or pid in seen:
            continue
        seen.add(pid)
        out.append({"address": a.get("ADDRESS"), "pid": int(pid)})
    return out


def get_parcel_by_pid(pid: int) -> dict:
    """Returns a GeoJSON Feature (WGS84) for the parcel, or raises."""
    d = _get(PARCEL_LAYER, {
        "where": f"PID = {int(pid)}",
        "outFields": "PID,PROP_ADD,COMP_AREA,MEAS_AREA,CAD_TYPE1,VOLUME,FOLIO",
        "returnGeometry": "true", "outSR": "4326", "f": "geojson",
    })
    feats = d.get("features", [])
    if not feats:
        raise ListApiError(f"No parcel found for PID {pid}")
    return _largest(feats)


def get_parcel_at_point(lon: float, lat: float) -> dict:
    """Returns the GeoJSON Feature of the parcel containing the point."""
    d = _get(PARCEL_LAYER, {
        "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "PID,PROP_ADD,COMP_AREA,MEAS_AREA,CAD_TYPE1,VOLUME,FOLIO",
        "returnGeometry": "true", "outSR": "4326", "f": "geojson",
    })
    feats = d.get("features", [])
    if not feats:
        raise ListApiError("No parcel found at that location")
    return _largest(feats)


def _largest(feats):
    """If multiple features (strata etc.), return the one with the largest computed area."""
    def area(f):
        return f.get("properties", {}).get("COMP_AREA") or 0
    return max(feats, key=area)
