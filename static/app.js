/* Subdivision Layout Generator — frontend (v2: multi-parcel, editable roads, terrain) */
"use strict";

const map = L.map("map").setView([-42.0, 147.0], 8);
const baseOSM = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "© OpenStreetMap",
}).addTo(map);
const baseSat = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 19, attribution: "© Esri" });
L.control.layers({ "Map": baseOSM, "Aerial": baseSat }, {}, { position: "topright" }).addTo(map);
if (map.pm) map.pm.setGlobalOptions({ allowSelfIntersection: false });

// ---------- state ----------
let parcels = [];              // [{feature, layer, key}]
let accessPoints = [];         // [{latlng, marker}]
let resultLayers = {};         // leaflet layers by name
let lastResult = null;         // last /api/generate response
let roadsDirty = false;        // user has dragged road handles
let mode = null;               // null | 'pick' | 'access'

const $ = (id) => document.getElementById(id);

// ---------- helpers ----------
async function jfetch(url, opts) {
  const r = await fetch(url, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}
function setMode(m) {
  mode = mode === m ? null : m;
  $("pickParcelBtn").classList.toggle("active", mode === "pick");
  $("accessBtn").classList.toggle("active", mode === "access");
  map.getContainer().style.cursor = mode ? "crosshair" : "";
}
function fmt(n) { return Number(n).toLocaleString(); }

// ---------- address search ----------
let searchTimer = null;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = $("search").value.trim();
  if (q.length < 4) { $("searchResults").classList.add("hidden"); return; }
  searchTimer = setTimeout(async () => {
    try {
      const d = await jfetch("/api/search?q=" + encodeURIComponent(q));
      const box = $("searchResults");
      box.innerHTML = "";
      d.results.forEach((r) => {
        const div = document.createElement("div");
        div.textContent = r.address;
        div.onclick = async () => {
          box.classList.add("hidden");
          $("search").value = r.address;
          try {
            const p = await jfetch("/api/parcel?pid=" + r.pid);
            addOrToggleParcel(p.feature, true);
          } catch (e) { alert(e.message); }
        };
        box.appendChild(div);
      });
      box.classList.toggle("hidden", d.results.length === 0);
    } catch (e) { console.warn(e); }
  }, 350);
});

// ---------- parcels (multi-select) ----------
function parcelKey(f) {
  return f.properties && f.properties.PID ? "pid:" + f.properties.PID
    : "geom:" + JSON.stringify(f.geometry.coordinates[0] && f.geometry.coordinates[0][0]);
}

function addOrToggleParcel(feature, zoom) {
  const key = parcelKey(feature);
  const existing = parcels.findIndex((p) => p.key === key);
  if (existing >= 0) { removeParcel(existing); return; }
  const layer = L.geoJSON(feature, { style: { color: "#1155cc", weight: 2.5, fillOpacity: 0.06 } }).addTo(map);
  parcels.push({ feature, layer, key });
  if (zoom || parcels.length === 1) map.fitBounds(layer.getBounds(), { padding: [30, 30] });
  clearResults();
  renderParcelList();
}

function removeParcel(i) {
  map.removeLayer(parcels[i].layer);
  parcels.splice(i, 1);
  clearResults();
  renderParcelList();
}

function renderParcelList() {
  const el = $("parcelList");
  if (!parcels.length) { el.classList.add("hidden"); $("generateBtn").disabled = true; return; }
  el.innerHTML = "";
  let total = 0;
  parcels.forEach((p, i) => {
    const props = p.feature.properties || {};
    total += props.COMP_AREA || 0;
    const div = document.createElement("div");
    div.className = "access-item";
    div.innerHTML = `<span title="${props.PROP_ADD || ""}">${(props.PROP_ADD || "Uploaded boundary").slice(0, 30)}</span>`;
    const btn = document.createElement("button");
    btn.textContent = "✕";
    btn.onclick = () => removeParcel(i);
    div.appendChild(btn);
    el.appendChild(div);
  });
  if (total > 0) {
    const sum = document.createElement("div");
    sum.innerHTML = `<b>${parcels.length} parcel(s), ~${(total / 10000).toFixed(2)} ha</b>`;
    el.appendChild(sum);
  }
  el.classList.remove("hidden");
  $("generateBtn").disabled = false;
}

$("pickParcelBtn").onclick = () => setMode("pick");
$("accessBtn").onclick = () => {
  if (!parcels.length) { alert("Select a parcel first"); return; }
  setMode("access");
};

map.on("click", async (e) => {
  if (mode === "pick") {
    try {
      const d = await jfetch(`/api/parcel?lon=${e.latlng.lng}&lat=${e.latlng.lat}`);
      addOrToggleParcel(d.feature, false);
    } catch (err) { alert(err.message); }
  } else if (mode === "access") {
    addAccessPoint(e.latlng);
  }
});

// ---------- access points ----------
function addAccessPoint(latlng) {
  const marker = L.circleMarker(latlng, { radius: 7, color: "#d97706", fillColor: "#f59e0b", fillOpacity: 0.9 }).addTo(map);
  accessPoints.push({ latlng, marker });
  renderAccessList();
}
function clearAccess() {
  accessPoints.forEach((a) => map.removeLayer(a.marker));
  accessPoints = [];
  renderAccessList();
}
function renderAccessList() {
  const el = $("accessList");
  if (!accessPoints.length) { el.classList.add("hidden"); return; }
  el.innerHTML = "";
  accessPoints.forEach((a, i) => {
    const div = document.createElement("div");
    div.className = "access-item";
    div.innerHTML = `<span>Access ${i + 1}</span>`;
    const btn = document.createElement("button");
    btn.textContent = "✕";
    btn.onclick = () => { map.removeLayer(a.marker); accessPoints.splice(i, 1); renderAccessList(); };
    div.appendChild(btn);
    el.appendChild(div);
  });
  el.classList.remove("hidden");
}

// ---------- upload ----------
$("shpUpload").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  try {
    const d = await jfetch("/api/upload_boundary", { method: "POST", body: fd });
    addOrToggleParcel(d.feature, true);
  } catch (err) { alert(err.message); }
  e.target.value = "";
});

// ---------- params UI ----------
function syncSliders() {
  $("targetVal").textContent = $("targetArea").value + " m²";
  $("depthVal").textContent = $("lotDepth").value + " m";
}
["targetArea", "lotDepth"].forEach((id) => $(id).addEventListener("input", syncSliders));
syncSliders();

$("zone").addEventListener("change", () => {
  const opt = $("zone").selectedOptions[0];
  const minA = Number(opt.dataset.min);
  $("targetArea").min = minA;
  $("targetArea").max = Math.max(minA * 4, 2500);
  if (Number($("targetArea").value) < minA) $("targetArea").value = Math.round(minA * 1.3);
  $("lotDepth").value = opt.dataset.depth;
  syncSliders();
});

$("autoAngle").addEventListener("change", () => {
  $("angleWrap").classList.toggle("hidden", $("autoAngle").checked);
});

// ---------- generate ----------
const LAYER_STYLE = {
  reserve: { color: "#777", weight: 0.8, fillColor: "#bbb", fillOpacity: 0.7 },
  carriageway: { color: "#555", weight: 0, fillColor: "#8a8a8a", fillOpacity: 0.9 },
  open_space: { color: "#6aa84f", weight: 1, fillColor: "#93c47d", fillOpacity: 0.5 },
  contours: { color: "#9b7653", weight: 0.7, opacity: 0.5 },
  streams: { color: "#3d85c6", weight: 2, opacity: 0.8, dashArray: "2 4" },
};

function clearResults() {
  Object.values(resultLayers).forEach((l) => map.removeLayer(l));
  resultLayers = {};
  lastResult = null;
  roadsDirty = false;
  $("resultsSection").classList.add("hidden");
  $("gradeReport").classList.add("hidden");
}

function currentParams() {
  return {
    zone: $("zone").value,
    target_lot_area: Number($("targetArea").value),
    lot_depth: Number($("lotDepth").value),
    road_preset: $("roadPreset").value,
    street_pattern: $("streetPattern").value,
    max_block_length: Number($("blockLen").value),
    road_angle_deg: $("autoAngle").checked ? null : (90 - Number($("roadAngle").value)),
    access_points: accessPoints.map((a) => [a.latlng.lng, a.latlng.lat]),
  };
}

async function runGenerate(roadsOverride) {
  if (!parcels.length) return;
  $("spinner").classList.remove("hidden");
  $("generateBtn").disabled = true;
  try {
    const body = {
      parcel_geometries: parcels.map((p) => p.feature.geometry),
      params: currentParams(),
    };
    if (roadsOverride) body.roads_override = roadsOverride;
    const d = await jfetch("/api/generate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    renderResult(d);
  } catch (e) {
    alert(e.message);
  } finally {
    $("spinner").classList.add("hidden");
    $("generateBtn").disabled = false;
  }
}

$("generateBtn").onclick = () => runGenerate(null);
$("resetBtn").onclick = () => runGenerate(null);
$("recalcBtn").onclick = () => {
  if (!resultLayers.roads) return;
  const fc = { type: "FeatureCollection", features: [] };
  resultLayers.roads.eachLayer((l) => fc.features.push(l.toGeoJSON()));
  runGenerate(fc);
};

function renderResult(d) {
  Object.values(resultLayers).forEach((l) => map.removeLayer(l));
  resultLayers = {};
  lastResult = d;
  roadsDirty = false;

  if (d.contours && d.contours.features.length) {
    resultLayers.contours = L.geoJSON(d.contours, {
      style: LAYER_STYLE.contours,
      onEachFeature: (f, l) => l.bindTooltip(`${f.properties.elev} m`, { sticky: true }),
    }).addTo(map);
  }
  ["open_space", "reserve", "carriageway"].forEach((k) => {
    if (d[k] && d[k].features.length) {
      resultLayers[k] = L.geoJSON(d[k], { style: LAYER_STYLE[k] }).addTo(map);
    }
  });
  if (d.streams && d.streams.features.length) {
    resultLayers.streams = L.geoJSON(d.streams, { style: LAYER_STYLE.streams }).addTo(map);
  }
  resultLayers.lots = L.geoJSON(d.lots, {
    style: (f) => ({
      color: f.properties.compliant ? "#b8860b" : "#cc4125",
      weight: 1.2,
      fillColor: f.properties.compliant ? "#ffe599" : "#f4cccc",
      fillOpacity: 0.45,
    }),
    onEachFeature: (f, l) => l.bindTooltip(
      `Lot ${f.properties.id}: ${fmt(f.properties.area_m2)} m², frontage ${f.properties.frontage_m} m` +
      (f.properties.steep ? " — steep" : "") +
      (f.properties.compliant ? "" : " — check compliance"), { sticky: true }),
  }).addTo(map);

  // roads last: editable with drag handles
  if (d.roads.features.length) {
    resultLayers.roads = L.geoJSON(d.roads, {
      style: { color: "#fff", weight: 3, dashArray: "8 5" },
      onEachFeature: (f, l) => {
        const g = f.properties.max_grade_pct != null ? `, max grade ${f.properties.max_grade_pct}%` : "";
        l.bindTooltip(`Road ${f.properties.id}: ${fmt(f.properties.length_m)} m${g} — drag to edit`, { sticky: true });
      },
    }).addTo(map);
    resultLayers.roads.eachLayer((l) => {
      if (l.pm) {
        l.pm.enable({ allowSelfIntersection: false });
        l.on("pm:edit", () => { roadsDirty = true; $("recalcBtn").classList.add("attention"); });
      }
    });
  }
  if (d.extras && d.extras.features.length) {
    resultLayers.extras = L.geoJSON(d.extras, {
      pointToLayer: (f, latlng) => L.circleMarker(latlng, {
        radius: 8, color: "#0b5394", fillColor: "#6fa8dc", fillOpacity: 0.9 }),
      onEachFeature: (f, l) => l.bindTooltip(
        `Suggested stormwater discharge (${f.properties.elev_m} m AHD)`, { sticky: true }),
    }).addTo(map);
  }
  parcels.forEach((p) => p.layer.bringToFront());

  const s = d.stats;
  const bearing = (((90 - s.road_angle_deg) % 180) + 180) % 180;
  $("stats").innerHTML =
    `<span>Lots</span><b>${s.lot_count} (${s.compliant_lots} compliant)</b>` +
    `<span>Yield</span><b>${s.yield_per_ha} lots/ha</b>` +
    `<span>Lot sizes</span><b>${fmt(s.min_lot_area)}–${fmt(s.max_lot_area)} m²</b>` +
    `<span>Average</span><b>${fmt(s.avg_lot_area)} m²</b>` +
    `<span>New road</span><b>${fmt(s.total_road_length_m)} m</b>` +
    `<span>Road take</span><b>${s.road_area_pct}%</b>` +
    `<span>Open space</span><b>${fmt(s.open_space_area_m2)} m²</b>` +
    `<span>Connectivity</span><b>${s.connectivity_index}</b>` +
    `<span>Road bearing</span><b>${bearing.toFixed(0)}° (${s.orientation_basis})</b>` +
    `<span>Terrain</span><b>${s.mean_slope_pct != null ? s.mean_slope_pct + "% — " : ""}${s.terrain_source}</b>`;
  $("warnings").innerHTML = d.warnings.map((w) => `<div>${w}</div>`).join("");
  $("recalcBtn").classList.remove("attention");
  $("gradesBtn").classList.toggle("hidden", !!(d.grades && d.grades.length));
  $("resultsSection").classList.remove("hidden");
}

// ---------- SRTM grades fallback (only when LIST terrain unavailable) ----------
$("gradesBtn").onclick = async () => {
  if (!lastResult) return;
  $("gradesBtn").disabled = true;
  $("gradesBtn").textContent = "Checking…";
  try {
    const d = await jfetch("/api/grades", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ roads: lastResult.roads }),
    });
    const rows = d.roads.map((r) =>
      `Road ${r.id}: max ${r.max_grade_pct}% ${r.ok ? (r.soft_ok ? "✓" : "⚠ >10%") : "✗ >17%"}`).join("<br>");
    $("gradeReport").innerHTML = `<b>${d.source}</b><br>${rows}` +
      (d.warnings.length ? "<br>" + d.warnings.join("<br>") : "");
    $("gradeReport").classList.remove("hidden");
  } catch (e) {
    $("gradeReport").innerHTML = "Elevation check failed: " + e.message;
    $("gradeReport").classList.remove("hidden");
  } finally {
    $("gradesBtn").disabled = false;
    $("gradesBtn").textContent = "Check road grades (SRTM)";
  }
};

// ---------- exports ----------
async function download(url, body, filename) {
  const r = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!r.ok) { const d = await r.json().catch(() => ({})); alert(d.error || r.statusText); return; }
  const blob = await r.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

$("exportShp").onclick = () => {
  if (!lastResult) return;
  download("/api/export/shp", {
    lots: lastResult.lots, roads: lastResult.roads, reserve: lastResult.reserve,
    open_space: lastResult.open_space, parcel: lastResult.parcel,
  }, "subdivision_layout.zip");
};

$("exportPdf").onclick = () => {
  if (!lastResult) return;
  const p = (parcels[0] && parcels[0].feature.properties) || {};
  download("/api/export/pdf", {
    layers: {
      lots: lastResult.lots, roads: lastResult.roads, reserve: lastResult.reserve,
      carriageway: lastResult.carriageway, open_space: lastResult.open_space, parcel: lastResult.parcel,
    },
    stats: lastResult.stats,
    meta: { address: p.PROP_ADD || "", pid: p.PID || "—" },
  }, "subdivision_concept_plan.pdf");
};
