# Generative Subdivision Layout App (Tasmania)

Flask web app that generates concept residential subdivision layouts (roads + lots)
compliant with the Tasmanian Planning Scheme (SPP), LGAT Tasmanian Infrastructure
Guidelines 2025 and the Tasmanian Municipal Standard Drawings v3.

## Features (v2)
- **Site selection**: search any Tasmanian address (LIST geocoder), click parcels on the
  map to build a **multi-parcel site** (dissolved automatically), or upload a zipped
  shapefile boundary.
- **Terrain-aware generation**: elevation model built from LIST contours (0.25 m LiDAR
  where available, 5 m statewide); road bearing chosen by trial-building networks and
  scoring estimated yield + contour alignment + solar orientation; road centrelines are
  **warped to follow contours** on sloped sites (gentle aesthetic curvature on flat ones);
  per-road max grades computed from the DEM.
- **Street-responsive lots**: lots are cut perpendicular to the local frontage tangent, so
  orientation varies around curves and corners; jittered frontage widths give a natural
  size mix; landlocked/under-served residue becomes balance land instead of fake lots.
- **Tweakable roads**: every road has drag handles (leaflet-geoman) — realign and hit
  *Recalculate lots from my roads* (terrain is cached, so regeneration takes ~2 s), or
  *Reset layout* to start over.
- **Context layers**: contours, mapped watercourses (crossing warnings per Natural Assets
  Code C7.0), suggested stormwater discharge point at the lowest boundary elevation.
- **Compliance**: lot area / frontage / 10×15 m building-envelope (incl. >1:5 slope core),
  solar orientation (SPP 8.6.1 A4), cul-de-sac length rules, road grade limits (10% / 17%),
  >100-lot collector warning, connectivity index; per-lot colour coding.
- **Exports**: zipped ESRI shapefile (GDA94/MGA55) and A3 PDF concept plan with scale bar,
  north arrow and title block.

Standards used by the engine: `STANDARDS_REFERENCE.md` (distilled from SPP/LGAT/TSD).
Urban-design heuristics and their sources: `DESIGN_PRINCIPLES.md`.

## Run locally
```
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000
python test_generate.py  # end-to-end smoke test (hits the live LIST API)
```

## Deploy to PythonAnywhere
1. Push this folder to GitHub, then on PythonAnywhere: `git clone` it.
2. `pip install --user -r requirements.txt` (or use a virtualenv).
3. Web tab → new Flask app → point the WSGI file at `app.py`'s `app` object:
   ```python
   import sys; sys.path.insert(0, '/home/<user>/<repo>')
   from app import app as application
   ```
4. **A paid account is required** — free-tier outbound internet is whitelist-only and
   `services.thelist.tas.gov.au` is not on it.

## Architecture
| Path | Purpose |
|---|---|
| `app.py` | Flask routes (search, parcel, upload, generate, grades, exports) |
| `generator/params.py` | Zone presets + road standards (the numbers) |
| `generator/layout.py` | Orchestration: terrain → roads → blocks → lots → compliance |
| `generator/roads.py` | Orientation scoring, grid, network pruning, contour warping |
| `generator/lots.py` | Frontage-perpendicular lot cutting (polygonise + merge) |
| `services/list_api.py` | LIST Tasmania ArcGIS REST (address search, cadastral parcels) |
| `services/dem.py` | LIST contour/stream fetch + IDW elevation grid + grade queries |
| `services/elevation.py` | OpenTopoData SRTM fallback grade check |
| `exporters/` | Shapefile (pyshp) and PDF (matplotlib) outputs |
| `static/`, `templates/` | Leaflet + leaflet-geoman single-page UI |

Geometry pipeline runs in GDA94 / MGA zone 55 (EPSG:28355); all I/O is WGS84 GeoJSON.

## Not yet implemented (roadmap)
- Sewer / water / stormwater concept layouts and connection points
- User-drawn flow paths / no-go areas; automatic road extension into balance land
- Open space placement strategy, collector hierarchy auto-assignment
- DEM GeoTIFF upload (LIST contours cover the state, so lower priority)

> Concept tool only — outputs require survey and council/TasWater approval.
> Cadastral data © State of Tasmania (the LIST).
