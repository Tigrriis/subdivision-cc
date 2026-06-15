"""Zone presets and generation parameters.

All numeric standards are sourced from STANDARDS_REFERENCE.md (SPP / LGAT TIG / TSD).
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ZonePreset:
    code: str
    name: str
    min_lot_area: float       # m2 (SPP Acceptable Solution)
    min_frontage: float       # m
    envelope_w: float         # building envelope rectangle, m
    envelope_d: float         # m
    front_setback: float      # m
    side_setback: float       # m
    rear_setback: float       # m
    default_lot_depth: float  # m, typical lot depth used by the generator
    max_envelope_slope: float = 0.20  # 1 in 5


ZONES = {
    "GRZ": ZonePreset("GRZ", "General Residential (8.0)", 450, 12, 10, 15, 4.5, 3.0, 4.0, 32),
    "IRZ": ZonePreset("IRZ", "Inner Residential (9.0)", 200, 3.6, 10, 12, 4.5, 3.0, 4.0, 25),
    "LDRZ": ZonePreset("LDRZ", "Low Density Residential (10.0)", 1500, 20, 10, 15, 8.0, 3.0, 4.0, 50),
    "VILLAGE": ZonePreset("VILLAGE", "Village (12.0)", 600, 12, 10, 15, 4.5, 3.0, 4.0, 36),
}

# Road standards (LGAT TIG Tables 1 & 4, TSD R06/R07)
ROAD_PRESETS = {
    # reserve, carriageway, footpath description
    "local_min": {"reserve": 18.0, "carriageway": 8.9, "label": "Local street (minimum, 18m reserve)"},
    "local_pref": {"reserve": 20.0, "carriageway": 8.9, "label": "Local street (preferred, 20m reserve)"},
    "collector": {"reserve": 20.0, "carriageway": 11.0, "label": "Collector (20m reserve)"},
}

# Street layout patterns (IRS Jul 2024: "rectilinear, modified or radiant grid preferred")
STREET_PATTERNS = {
    "rectilinear": "Rectilinear grid (straight, most legible)",
    "modified": "Modified grid (grid + terrain curvature)",
    "radiant": "Radiant grid (radiates from access/focal point)",
    "organic": "Organic (curvilinear, follows contours)",
}

CULDESAC_HEAD_KERB_RADIUS = 9.0       # 18.0m dia face of kerb (TSD R07)
CULDESAC_HEAD_RESERVE_RADIUS = 12.5   # 25m reserve dia at head (TSD R07)
CULDESAC_SHORT_MAX_LEN = 150.0        # <=150m allows reduced section (TIG Table 4)
CULDESAC_BUSHFIRE_MAX_LEN = 200.0     # >200m triggers C13.0 requirements
MAX_ROAD_GRADE = 0.17                 # TIG 3.4.5
TARGET_ROAD_GRADE = 0.10              # bus/heavy vehicle limit, used as a soft target
MIN_ROAD_GRADE = 0.005
SOLAR_AXIS_TOLERANCE_DEG = 30.0       # SPP 8.6.1 A4: lot long axis within 30 deg of true north


@dataclass
class GenParams:
    zone: str = "GRZ"
    target_lot_area: float = 600.0     # m2
    lot_depth: float = 0.0             # 0 -> use zone default
    road_preset: str = "local_min"
    road_angle_deg: float | None = None  # bearing of road direction (deg from East, math convention); None = auto
    max_block_length: float = 180.0    # m between cross streets (IRS: 120-240m, >200m sparingly)
    street_pattern: str = "modified"   # see STREET_PATTERNS
    access_points: list = field(default_factory=list)  # [(lon, lat), ...]

    def zone_preset(self) -> ZonePreset:
        return ZONES[self.zone]

    def road(self) -> dict:
        return ROAD_PRESETS[self.road_preset]

    def depth(self) -> float:
        return self.lot_depth if self.lot_depth and self.lot_depth > 0 else self.zone_preset().default_lot_depth

    @classmethod
    def from_dict(cls, d: dict) -> "GenParams":
        p = cls()
        if d.get("zone") in ZONES:
            p.zone = d["zone"]
        if d.get("road_preset") in ROAD_PRESETS:
            p.road_preset = d["road_preset"]
        if d.get("street_pattern") in STREET_PATTERNS:
            p.street_pattern = d["street_pattern"]
        for k in ("target_lot_area", "lot_depth", "max_block_length"):
            if d.get(k) is not None:
                setattr(p, k, float(d[k]))
        if d.get("road_angle_deg") is not None and d.get("road_angle_deg") != "":
            p.road_angle_deg = float(d["road_angle_deg"])
        p.access_points = [(float(a[0]), float(a[1])) for a in d.get("access_points", [])]
        # never allow target below the zone Acceptable Solution minimum
        p.target_lot_area = max(p.target_lot_area, p.zone_preset().min_lot_area)
        return p
