# Generative Subdivision Layout App (Tasmania)

Flask web app that generates concept residential subdivision layouts (roads + lots)
compliant with the Tasmanian Planning Scheme (SPP), LGAT Tasmanian Infrastructure
Guidelines 2025 and the Tasmanian Municipal Standard Drawings v3.

## Features (v1)
- **Site selection**: search any Tasmanian address (LIST geocoder), click a parcel on the
  map, or upload a zipped shapefile boundary.
- **Generation**: road grid + double-loaded blocks + lots, oriented automatically or by a
  user bearing; user-set access points on the boundary; cul-de-sac heads per TSD R07.
- **Parameters**: zone preset (GRZ / IRZ / LDRZ / Village), target lot size, lot depth,
  street type (18 m min / 20 m preferred reserve), max block length.
- **Compliance**: lot area / frontage / 10×15 m building-envelope checks, solar orientation
  (SPP 8.6.1 A4), cul-de-sac length rules, >100-lot collector warning; per-lot colour coding.
- **Road grades**: optional check against SRTM 30 m elevation (indicative).
- **Exports**: zipped ESRI shapefile (GDA94/MGA55) and A3 PDF concept plan with scale bar,
  north arrow and title block.

All standards used by the engine are documented in `STANDARDS_REFERENCE.md`
(distilled from the reference PDFs in this folder).

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
| `generator/layout.py` | Layout engine: road grid → network pruning → blocks → lots |
| `services/list_api.py` | LIST Tasmania ArcGIS REST (address search, cadastral parcels) |
| `services/elevation.py` | OpenTopoData SRTM sampling + TIG grade checks |
| `exporters/` | Shapefile (pyshp) and PDF (matplotlib) outputs |
| `static/`, `templates/` | Leaflet single-page UI |

Geometry pipeline runs in GDA94 / MGA zone 55 (EPSG:28355); all I/O is WGS84 GeoJSON.

## Not yet implemented (roadmap)
- Sewer / water / stormwater concept layouts and connection points
- DEM upload + flow-path avoidance; grade-aware road routing
- Open space placement strategy, collector hierarchy auto-assignment
- Editable road alignments (drag) with live regeneration

> Concept tool only — outputs require survey and council/TasWater approval.
> Cadastral data © State of Tasmania (the LIST).
