# Subdivision Design Principles → Engine Heuristics

How recognised land-development and urban-design concepts are translated into concrete
rules in the layout engine. Compliance numbers live in `STANDARDS_REFERENCE.md`; this file
covers *quality* of layout. Sources: Charter of the New Urbanism; Grammenos & Pogharian,
*Residential Street Pattern Design* (Fused Grid, CMHC); APA PAS Report 126 *Hillside
Development*; San Diego Steep Hillside Guidelines; VDOT Subdivision Street Design Guide;
Austroads Movement & Place; Tas. *Improving Residential Standards* report (2024).

## 1. Street network character
| Principle | Source concept | Engine rule |
|---|---|---|
| Connected network, dispersed traffic, route choice | New Urbanism / Fused Grid | Prefer through-streets and loops; cul-de-sacs only where parcels or terrain force dead ends. Connectivity index (links/nodes) reported; target ≥ 1.4. |
| Walkable blocks | New Urbanism (block perimeter ≤ ~500 m) | Block length capped (default 220 m, user 80–400 m); cross-streets inserted automatically. |
| Streets follow terrain, not fight it | Hillside guidelines (APA PAS 126; San Diego) | Roads are nudged along contours (constant-elevation seeking within a corridor) and smoothed; grid axis chosen so primary streets run across-slope, not down-slope. |
| Gentle curvature, no relentless straights | Garden suburb / TND character | Even on flat sites roads receive subtle low-amplitude curvature; curve radii kept drivable (≥ 50 m local). |
| Climb diagonally, not straight up | Hillside guidelines | When slope > ~8 %, grid orientation rotates toward the contour direction so streets traverse the hill at an angle. |
| Terminated vistas / legible places | TND | Open-space residues preferentially placed at T-junction ends and block ends (reported as open space rather than forced lots). |
| Streets as places, not just movement | Movement & Place | Reserve presets keep verges for trees/footpaths; short cul-de-sacs get reduced section (TIG) to read as lanes. |

## 2. Lots
| Principle | Source concept | Engine rule |
|---|---|---|
| Lots front the street they belong to | Perimeter block urbanism | Lots are cut perpendicular to the *local frontage tangent*, so orientation varies around curves and corners — no single global grid orientation. |
| Diversity of lot sizes | New Urbanism (mix of housing) | Frontage widths are jittered ±10 % around the target and corner/end lots absorb remainders, producing a natural size distribution (still ≥ zone minimum). |
| Solar access | SPP 8.6.1 A4 + passive solar design | Orientation scoring prefers street bearings that keep lot long axes within 30° of N where terrain allows; deviation reported as a warning, never silently ignored. |
| Build with the slope | Hillside guidelines | Lot envelope check uses the DEM: lots whose buildable core exceeds 1:5 grade are flagged (SPP envelope rule). |
| Back-to-back lots, no left-over slivers | Subdivision practice | Double-loaded blocks split along a spine equidistant from frontages; sub-minimum residues merge into neighbours or become open space. |

## 3. Terrain & drainage
| Principle | Source concept | Engine rule |
|---|---|---|
| Minimise cut & fill | Hillside guidelines | Roads follow contours within a lateral corridor (≤ ~35 % of block depth deviation) before smoothing; grade re-checked from DEM after routing. |
| Respect natural flow paths | WSUD / TIG major drainage | Valley lines (local elevation minima) are detected from the DEM; roads crossing them get a warning so culverts/OLFPs can be considered. *(v2: detection + warning; full avoidance is roadmap.)* |
| Drainage to the low point | TIG stormwater | Lowest boundary point reported as the suggested legal discharge / detention location. |

## 4. What the engine deliberately does NOT do
- No mixed-use centres, lot-by-lot housing types or staging — out of scope for a lot-layout tool.
- No guarantee of optimality: the engine generates a *defensible starting point*; the road
  handles exist precisely so a designer can impose intent the heuristics can't know.

Sources: [Principles of New Urbanism (Seaside Institute)](https://seasideinstitute.org/principles-of-new-urbanism/) ·
[Grammenos & Pogharian — Residential Street Pattern Design](https://realestate.wharton.upenn.edu/wp-content/uploads/2017/03/389.pdf) ·
[APA PAS 126 — Hillside Development](https://www.planning.org/pas/reports/report126.htm) ·
[San Diego Steep Hillside Guidelines](https://www.sandiego.gov/sites/default/files/legacy/development-services/pdf/industry/landdevmanual/ldmsteephillsides.pdf) ·
[VDOT Subdivision Street Design Guide](https://www.vdot.virginia.gov/media/vdotvirginiagov/doing-business/technical-guidance-and-support/land-use-and-development/subdivision-street-requirements/appendb.pdf)
