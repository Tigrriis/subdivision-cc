/* Subdivision Layout Generator — frontend */
"use strict";

const map = L.map("map").setView([-42.0, 147.0], 8);
const baseOSM = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: "© OpenStreetMap",
}).addTo(map);
const baseSat = L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
  { maxZoom: 19, attribution: "© Esri" });
L.control.layers({ "Map": baseOSM, "Aerial": baseSat }, {}, { position: "topright" }).addTo(map);

// ---------- state ----------
let parcelFeature = null;      // GeoJSON feature (WGS84)
let parcelLayer = null;
let accessPoints = [];         // [{latlng, marker}]
let resultLayers = {};         // leaflet layers by name
let lastResult = null;         // last /api/generate response
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
        div.onclick = () => { box.classList.add("hidden"); $("search").value = r.address; loadParcel({ pid: r.pid }); };
        box.appendChild(div);
      });
      box.classList.toggle("hidden", d.results.length === 0);
    } catch (e) { console.warn(e); }
  }, 350);
});

// ---------- parcel loading ----------
async function loadParcel(q) {
  try {
    const qs = q.pid ? "pid=" + q.pid : `lon=${q.lon}&lat=${q.lat}`;
    const d = await jfetch("/api/parcel?" + qs);
    setParcel(d.feature);
  } catch (e) { alert(e.message); }
}

function setParcel(feature) {
  parcelFeature = feature;
  clearResults();
  clearAccess();
  if (parcelLayer) map.removeLayer(parcelLayer);
  parcelLayer = L.geoJSON(feature, { style: { color: "#1155cc", weight: 2.5, fillOpacity: 0.06 } }).addTo(map);
  map.fitBounds(parcelLayer.getBounds(), { padding: [30, 30] });
  const p = feature.properties || {};
  $("parcelInfo").innerHTML =
    `<b>${p.PROP_ADD || "Uploaded boundary"}</b><br>` +
    (p.PID ? `PID: ${p.PID}<br>` : "") +
    (p.COMP_AREA ? `Area: ${fmt(Math.round(p.COMP_AREA))} m² (${(p.COMP_AREA / 10000).toFixed(2)} ha)` : "") +
    (p.crs_assumed ? "<br>⚠ No .prj in upload — assumed GDA94/MGA55" : "");
  $("parcelInfo").classList.remove("hidden");
  $("generateBtn").disabled = false;
}

$("pickParcelBtn").onclick = () => setMode("pick");
$("accessBtn").onclick = () => {
  if (!parcelFeature) { alert("Select a parcel first"); return; }
  setMode("access");
};

map.on("click", (e) => {
  if (mode === "pick") {
    setMode(null);
    loadParcel({ lon: e.latlng.lng, lat: e.latlng.lat });
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
    setParcel(d.feature);
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
  open_space: { color: "#6aa84f", weight: 1, fillColor: "#93c47d", fillOpacity: 0.55 },
};

function clearResults() {
  Object.values(resultLayers).forEach((l) => map.removeLayer(l));
  resultLayers = {};
  lastResult = null;
  $("resultsSection").classList.add("hidden");
  $("gradeReport").classList.add("hidden");
}

$("generateBtn").onclick = async () => {
  if (!parcelFeature) return;
  $("spinner").classList.remove("hidden");
  $("generateBtn").disabled = true;
  try {
    const params = {
      zone: $("zone").value,
      target_lot_area: Number($("targetArea").value),
      lot_depth: Number($("lotDepth").value),
      road_preset: $("roadPreset").value,
      max_block_length: Number($("blockLen").value),
      road_angle_deg: $("autoAngle").checked ? null : (90 - Number($("roadAngle").value)),
      access_points: accessPoints.map((a) => [a.latlng.lng, a.latlng.lat]),
    };
    const d = await jfetch("/api/generate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ parcel_geometry: parcelFeature.geometry, params }),
    });
    renderResult(d);
  } catch (e) {
    alert(e.message);
  } finally {
    $("spinner").classList.add("hidden");
    $("generateBtn").disabled = false;
  }
};

function renderResult(d) {
  Object.values(resultLayers).forEach((l) => map.removeLayer(l));
  resultLayers = {};
  lastResult = d;

  ["open_space", "reserve", "carriageway"].forEach((k) => {
    if (d[k] && d[k].features.length) {
      resultLayers[k] = L.geoJSON(d[k], { style: LAYER_STYLE[k] }).addTo(map);
    }
  });
  if (d.roads.features.length) {
    resultLayers.roads = L.geoJSON(d.roads, {
      style: { color: "#fff", weight: 1.6, dashArray: "8 5" },
    }).addTo(map);
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
      (f.properties.compliant ? "" : " — check compliance"), { sticky: true }),
  }).addTo(map);
  if (parcelLayer) parcelLayer.bringToFront();

  const s = d.stats;
  $("stats").innerHTML =
    `<span>Lots</span><b>${s.lot_count} (${s.compliant_lots} compliant)</b>` +
    `<span>Yield</span><b>${s.yield_per_ha} lots/ha</b>` +
    `<span>Lot sizes</span><b>${fmt(s.min_lot_area)}–${fmt(s.max_lot_area)} m²</b>` +
    `<span>Average</span><b>${fmt(s.avg_lot_area)} m²</b>` +
    `<span>New road</span><b>${fmt(s.total_road_length_m)} m</b>` +
    `<span>Road take</span><b>${s.road_area_pct}%</b>` +
    `<span>Open space</span><b>${fmt(s.open_space_area_m2)} m²</b>` +
    `<span>Road bearing</span><b>${(((90 - s.road_angle_deg) % 180 + 180) % 180).toFixed(0)}°</b>`;
  $("warnings").innerHTML = d.warnings.map((w) => `<div>${w}</div>`).join("");
  $("resultsSection").classList.remove("hidden");
}

// ---------- grades ----------
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
  const p = parcelFeature.properties || {};
  download("/api/export/pdf", {
    layers: {
      lots: lastResult.lots, roads: lastResult.roads, reserve: lastResult.reserve,
      carriageway: lastResult.carriageway, open_space: lastResult.open_space, parcel: lastResult.parcel,
    },
    stats: lastResult.stats,
    meta: { address: p.PROP_ADD || "", pid: p.PID || "—" },
  }, "subdivision_concept_plan.pdf");
};
