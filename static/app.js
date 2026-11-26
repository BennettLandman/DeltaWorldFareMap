/* Delta Fare Map front end: plain JS + Leaflet, no build step. */
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => (n == null ? "—" : "$" + Math.round(n).toLocaleString("en-US"));
const dur = (m) => (m == null ? "—" : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`);
const stopsTxt = (n) => (n == null ? "—" : n === 0 ? "Nonstop" : n === 1 ? "1 stop" : `${n} stops`);

const S = {
  config: null, home: null, query: null,
  dests: new Map(),             // code -> destination
  regionsLoaded: 0, regionErrors: [],
  filter: { max: null, stops: "any", regions: new Set() },  // regions: enabled = searched + shown
  loaded: new Set(),            // regions loaded for the current query
  sort: { k: "price", desc: false },
  city: null, out: null, outSel: null, ret: null, retSel: null,
  es: null,
};

/* ---------------- map ---------------- */
const map = L.map("map", { minZoom: 2, worldCopyJump: false, zoomSnap: 0.5 }).setView([30, -40], 2);
const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/";
L.tileLayer(ESRI + "World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
  attribution: "Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ", maxZoom: 12,
}).addTo(map);
const routeLayer = L.layerGroup().addTo(map);
const markerLayer = L.layerGroup().addTo(map);
const labelLayer = L.layerGroup().addTo(map);
let homeMarker = null;
const legendCtl = L.control({ position: "bottomleft" });
legendCtl.onAdd = () => { const el = $("#legend"); L.DomEvent.disableClickPropagation(el); return el; };
legendCtl.addTo(map);
const markers = new Map();

// Put a longitude on the world copy nearest the home airport so the map is home-centred.
const nearLon = (lon) => {
  const h = S.home ? S.home.lon : -40;
  while (lon - h > 180) lon -= 360;
  while (h - lon > 180) lon += 360;
  return lon;
};

function greatCircle(a, b, n = 48) {
  const r = Math.PI / 180, [la1, lo1, la2, lo2] = [a[0] * r, a[1] * r, b[0] * r, b[1] * r];
  const d = 2 * Math.asin(Math.sqrt(Math.sin((la2 - la1) / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin((lo2 - lo1) / 2) ** 2));
  if (d < 1e-6) return [a, b];
  const pts = [];
  for (let i = 0; i <= n; i++) {
    const f = i / n, A = Math.sin((1 - f) * d) / Math.sin(d), B = Math.sin(f * d) / Math.sin(d);
    const x = A * Math.cos(la1) * Math.cos(lo1) + B * Math.cos(la2) * Math.cos(lo2);
    const y = A * Math.cos(la1) * Math.sin(lo1) + B * Math.cos(la2) * Math.sin(lo2);
    const z = A * Math.sin(la1) + B * Math.sin(la2);
    pts.push([Math.atan2(z, Math.sqrt(x * x + y * y)) / r, Math.atan2(y, x) / r]);
  }
  // unwrap so the line is continuous, anchored near home
  pts[0][1] = nearLon(pts[0][1]);
  for (let i = 1; i < pts.length; i++) {
    while (pts[i][1] - pts[i - 1][1] > 180) pts[i][1] -= 360;
    while (pts[i - 1][1] - pts[i][1] > 180) pts[i][1] += 360;
  }
  return pts;
}

/* color: rank-based green -> red across the visible set */
const STOPS = [[26, 152, 80], [145, 207, 96], [254, 224, 139], [252, 141, 89], [215, 48, 39]];
function ramp(t) {
  t = Math.max(0, Math.min(1, t)) * (STOPS.length - 1);
  const i = Math.min(STOPS.length - 2, Math.floor(t)), f = t - i, a = STOPS[i], b = STOPS[i + 1];
  return `rgb(${a.map((v, k) => Math.round(v + (b[k] - v) * f)).join(",")})`;
}

function visibleDests() {
  const f = S.filter;
  return [...S.dests.values()].filter((d) =>
    (f.max == null || d.price <= f.max) &&
    (f.stops === "any" || (d.stops ?? 9) <= Number(f.stops)) &&
    f.regions.has(d.region));
}

function renderMap() {
  markerLayer.clearLayers(); markers.clear();
  const vis = visibleDests().sort((a, b) => a.price - b.price);
  const n = vis.length;
  vis.forEach((d, i) => {
    const color = ramp(n > 1 ? i / (n - 1) : 0);
    const sel = S.city && S.city.code === d.code;
    const m = L.circleMarker([d.lat, nearLon(d.lon)], {
      radius: sel ? 9 : 6.5, color: sel ? "#1d2433" : "#fff", weight: sel ? 2.5 : 1.2, fillColor: color, fillOpacity: 0.95,
    });
    m.bindTooltip(
      `<b>${esc(d.city)}</b>, ${esc(d.country)} (${esc(d.code)})<br>${money(d.price)} round trip` +
      `${d.fare_is_main === "yes" ? " · Main Cabin" : ""}<br>${stopsTxt(d.stops)} · ${dur(d.minutes)} each way` +
      `${d.airline && d.airline !== "Delta" ? "<br>" + esc(d.airline) : ""}`,
      { className: "tip", direction: "top", offset: [0, -6] });
    m.on("click", () => openCity(d.code));
    m.addTo(markerLayer);
    markers.set(d.code, { m, d, color });
  });
  if (S.home) {
    if (!homeMarker) {
      homeMarker = L.marker([S.home.lat, S.home.lon], {
        icon: L.divIcon({ className: "home-star", html: "★", iconSize: [26, 26], iconAnchor: [13, 13] }),
        zIndexOffset: 1000, keyboard: false,
      }).addTo(map);
    }
    homeMarker.setLatLng([S.home.lat, S.home.lon]);
    homeMarker.bindTooltip(`<b>${esc(S.home.code)}</b> ${esc(S.home.name)}`, { className: "tip", direction: "top" });
  }
  const leg = $("#legend");
  if (n) {
    leg.hidden = false;
    leg.innerHTML = `Lowest round-trip fare<div class="bar"></div><div class="ends"><span>${money(vis[0].price)}</span><span>${money(vis[n - 1].price)}</span></div>`;
  } else leg.hidden = true;
  renderLabels();
  renderList();
}

// Show fare labels where they fit, cheapest first, skipping any that would collide.
function renderLabels() {
  labelLayer.clearLayers();
  const placed = [], size = map.getSize();
  const items = [...markers.values()].sort((a, b) => a.d.price - b.d.price);
  for (const { d } of items) {
    const p = map.latLngToContainerPoint([d.lat, nearLon(d.lon)]);
    const text = money(d.price), w = 10 + text.length * 7, h = 17;
    const box = { x: p.x + 8, y: p.y - h / 2, w, h };
    if (box.x < 0 || box.y < 0 || box.x + w > size.x || box.y + h > size.y) continue;
    const pad = 2;
    if (placed.some((b) => box.x < b.x + b.w + pad && box.x + box.w + pad > b.x && box.y < b.y + b.h + pad && box.y + box.h + pad > b.y)) continue;
    // also keep clear of other markers' dots
    placed.push(box);
    L.marker([d.lat, nearLon(d.lon)], {
      icon: L.divIcon({ className: "", html: `<div class="fare-label ${d.fare_is_main === "yes" ? "main" : ""}">${text}</div>`, iconSize: [w, h], iconAnchor: [-8, h / 2] }),
      interactive: false, keyboard: false,
    }).addTo(labelLayer);
  }
}
map.on("zoomend moveend resize", renderLabels);

/* ---------------- list view ---------------- */
const REGION_NAME = () => Object.fromEntries((S.config?.regions || []).map((r) => [r.id, r.name]));
function renderList() {
  const rows = visibleDests(), { k, desc } = S.sort, names = REGION_NAME();
  rows.sort((a, b) => {
    const x = a[k] ?? Infinity, y = b[k] ?? Infinity;
    const c = typeof x === "string" ? x.localeCompare(y) : x - y;
    return desc ? -c : c;
  });
  $("#count").textContent = S.dests.size ? `(${rows.length} of ${S.dests.size} cities)` : "";
  $("#list tbody").innerHTML = rows.map((d) => `<tr data-code="${esc(d.code)}">
    <td>${esc(d.city)}</td><td>${esc(d.country)}</td><td>${esc(d.code)}</td>
    <td class="num">${money(d.price)}${d.fare_is_main === "yes" ? " ✓" : ""}</td>
    <td class="num">${stopsTxt(d.stops)}</td><td class="num">${dur(d.minutes)}</td><td>${esc(names[d.region] || d.region)}</td></tr>`).join("");
  document.querySelectorAll("#list th").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.k === k);
    th.classList.toggle("desc", th.dataset.k === k && desc);
  });
}
$("#list thead").addEventListener("click", (e) => {
  const k = e.target.dataset.k; if (!k) return;
  S.sort = { k, desc: S.sort.k === k ? !S.sort.desc : false };
  renderList();
});
$("#list tbody").addEventListener("click", (e) => {
  const tr = e.target.closest("tr"); if (tr) openCity(tr.dataset.code);
});

/* ---------------- filters ---------------- */
function priceBounds() {
  const p = [...S.dests.values()].map((d) => d.price);
  return p.length ? [Math.min(...p), Math.max(...p)] : [0, 0];
}
function readMax() {
  const v = Number($("#maxprice").value);
  if (v >= 100 || !S.dests.size) { S.filter.max = null; $("#maxv").textContent = "any"; return; }
  const [lo, hi] = priceBounds();
  S.filter.max = Math.round(lo + (hi - lo) * (v / 100));
  $("#maxv").textContent = money(S.filter.max);
}
$("#maxprice").addEventListener("input", () => { readMax(); renderMap(); });
$("#stops").addEventListener("change", (e) => { S.filter.stops = e.target.value; renderMap(); });
function renderChips() {
  $("#chips").innerHTML = S.config.regions.map((r) => {
    const on = S.filter.regions.has(r.id), pending = on && S.query && !S.loaded.has(r.id);
    return `<button type="button" class="chip ${on ? "on" : ""}" data-r="${r.id}"
      title="${on ? "Click to hide and skip in searches" : "Click to include (1 search if not cached)"}">${esc(r.name)}${pending ? " …" : ""}</button>`;
  }).join("");
}
$("#chips").addEventListener("click", (e) => {
  const r = e.target.closest(".chip")?.dataset.r; if (!r) return;
  S.filter.regions.has(r) ? S.filter.regions.delete(r) : S.filter.regions.add(r);
  localStorage.setItem("regions", [...S.filter.regions].join(","));
  renderChips(); renderMap(); updateCost();
  // Turning a region on after a search loads just that region.
  if (S.filter.regions.has(r) && S.query && !S.loaded.has(r) && !S.es) loadRegions([r]);
});

// Show what the next Search would cost (cheap local call; never uses a search).
let costTimer = null;
function updateCost() {
  clearTimeout(costTimer);
  costTimer = setTimeout(async () => {
    const origin = $("#origin").value.trim().split(/\s/)[0].toUpperCase();
    const depart = $("#depart").value, ret = $("#ret").value, regs = [...S.filter.regions];
    const go = $("#go");
    if (!regs.length) { go.textContent = "Search"; go.title = "Pick at least one region"; return; }
    if (!origin || !depart || !ret || ret <= depart) { go.textContent = "Search"; return; }
    const r = await getJSON(`/api/estimate?origin=${encodeURIComponent(origin)}&depart=${depart}&ret=${ret}&regions=${regs.join(",")}`);
    if (!r.ok) { go.textContent = "Search"; return; }
    const n = r.body.live_calls;
    go.textContent = r.body.provider === "demo" ? "Search" : n ? `Search · ${n} search${n > 1 ? "es" : ""}` : "Search · cached";
  }, 150);
}
["#depart", "#ret"].forEach((sel) => $(sel).addEventListener("change", updateCost));
$("#origin").addEventListener("change", updateCost);

/* ---------------- quota ---------------- */
function renderQuota(q) {
  const el = $("#quota");
  if (!q) return;
  if (q.provider === "demo") {
    el.innerHTML = `<span class="demo" title="${esc(q.key_problem || "DELTAMAP_PROVIDER=demo")}">DEMO DATA: fares are invented</span>` +
      (q.key_problem ? `<div>${esc(q.key_problem)}</div>` : "");
    return;
  }
  if (!q.ok) { el.textContent = q.error || "Quota unavailable"; return; }
  const low = q.left != null && q.left < q.low_warn;
  el.innerHTML = `<b style="${low ? "color:var(--accent)" : ""}">${q.left ?? "?"}</b> of ${q.per_month ?? "?"} searches left this month` +
    (q.hour_left != null ? `<br>${q.hour_left} left this hour` : "");
}
async function refreshQuota(force = false) {
  try { renderQuota(await (await fetch(`/api/quota${force ? "?refresh=1" : ""}`)).json()); } catch { /* ignore */ }
}

/* ---------------- search ---------------- */
function setStatus(html) { $("#status").innerHTML = html; }

async function getJSON(url) {
  const r = await fetch(url);
  let body; try { body = await r.json(); } catch { body = {}; }
  return { ok: r.ok, status: r.status, body };
}

// Fetch a drill-down endpoint, asking the user first when the backend says quota is low.
async function guarded(url) {
  let r = await getJSON(url);
  if (r.status === 409 && r.body.confirm_needed) {
    if (!window.confirm(r.body.message)) return { cancelled: true };
    r = await getJSON(url + "&confirm=1");
  }
  if (r.status === 409) return { error: r.body.message || "Blocked by quota guardrail" };
  if (!r.ok) return { error: r.body.detail || `HTTP ${r.status}` };
  return { data: r.body };
}

$("#search").addEventListener("submit", async (e) => {
  e.preventDefault();
  hideAC();
  const origin = $("#origin").value.trim().split(/\s/)[0].toUpperCase();
  const depart = $("#depart").value, ret = $("#ret").value;
  if (!depart || !ret) return setStatus(`<span class="err">Pick both dates.</span>`);
  if (ret <= depart) return setStatus(`<span class="err">Return must be after departure.</span>`);
  if (!S.filter.regions.size) return setStatus(`<span class="err">Pick at least one region.</span>`);
  const qs = `origin=${encodeURIComponent(origin)}&depart=${depart}&ret=${ret}`;
  const regs = [...S.filter.regions];
  const est = await getJSON(`/api/estimate?${qs}&regions=${regs.join(",")}`);
  if (!est.ok) return setStatus(`<span class="err">${esc(est.body.detail || "Search failed")}</span>`);
  let confirm = false;
  const g = est.body.guard;
  if (g && g.confirm_needed) {
    if (!window.confirm(g.message)) return setStatus("Search cancelled.");
    confirm = true;
  }
  const home = await getJSON(`/api/airport/${origin}`);
  S.home = home.body; $("#origin").value = `${origin} · ${S.home.city || S.home.name}`;
  S.query = { origin, depart, ret };
  history.replaceState(null, "", `?${qs}`);
  closePanel();
  S.dests.clear(); S.loaded.clear();
  routeLayer.clearLayers();
  map.setView([25, S.home.lon], 2);
  startExplore(qs, regs, confirm, est.body);
});

// Load extra regions into the current map (after a chip is switched on).
async function loadRegions(regs) {
  const q = S.query, qs = `origin=${q.origin}&depart=${q.depart}&ret=${q.ret}`;
  const est = await getJSON(`/api/estimate?${qs}&regions=${regs.join(",")}`);
  if (!est.ok) return;
  let confirm = false;
  if (est.body.guard?.confirm_needed) {
    if (!window.confirm(est.body.guard.message)) return;
    confirm = true;
  }
  startExplore(qs, regs, confirm, est.body);
}

function startExplore(qs, regs, confirm, est) {
  if (S.es) S.es.close();
  S.regionsLoaded = 0; S.regionErrors = [];
  renderMap(); renderChips();
  $("#go").disabled = true;
  const total = regs.length;
  const cost = est.provider === "demo" ? "demo data" : est.live_calls ? `uses ${est.live_calls} search${est.live_calls > 1 ? "es" : ""}` : "all from cache, 0 searches";
  const progress = () => {
    const errs = S.regionErrors.length ? ` · <span class="err">${S.regionErrors.map(esc).join("; ")}</span>` : "";
    setStatus(`${S.regionsLoaded} of ${total} regions loaded · ${S.dests.size} cities · ${cost}${errs}`);
  };
  progress();
  const es = new EventSource(`/api/explore?${qs}&regions=${regs.join(",")}${confirm ? "&confirm=1" : ""}`);
  S.es = es;
  let finished = false;
  es.addEventListener("confirm", (ev) => {
    es.close(); $("#go").disabled = false;
    const b = JSON.parse(ev.data);
    S.es = null;
    if (window.confirm(b.message)) startExplore(qs, regs, true, est); else setStatus("Search cancelled.");
  });
  es.addEventListener("region", (ev) => {
    const res = JSON.parse(ev.data);
    S.regionsLoaded++;
    if (!res.error) S.loaded.add(res.region);
    if (res.error) S.regionErrors.push(`${REGION_NAME()[res.region] || res.region}: ${res.error}`);
    for (const d of res.destinations) {
      const cur = S.dests.get(d.code);
      if (!cur || d.price < cur.price) S.dests.set(d.code, d);
    }
    readMax(); renderMap(); renderChips(); progress();
  });
  es.addEventListener("done", (ev) => {
    finished = true; es.close(); S.es = null; $("#go").disabled = false;
    renderQuota(JSON.parse(ev.data)); progress(); updateCost();
    if (!S.dests.size && !S.regionErrors.length) setStatus("No Delta destinations found for these dates.");
  });
  es.onerror = () => {
    if (finished) return;
    es.close(); S.es = null; $("#go").disabled = false;
    S.regionErrors.push("connection to the local server was lost"); progress();
  };
}

/* ---------------- city panel ---------------- */
function closePanel() {
  $("#panel").classList.remove("open"); $("#panel").setAttribute("aria-hidden", "true");
  S.city = null; S.out = S.outSel = S.ret = S.retSel = null;
  routeLayer.clearLayers(); renderMap();
}
$("#close").addEventListener("click", closePanel);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && S.city) closePanel(); });

function fmtTime(t) {
  if (!t) return "—";
  const [, hm] = t.split(" "); let [h, m] = hm.split(":").map(Number);
  const ap = h >= 12 ? "PM" : "AM"; h = h % 12 || 12;
  return `${h}:${String(m).padStart(2, "0")} ${ap}`;
}
function dayDiff(a, b) {
  return Math.round((new Date(b.split(" ")[0]) - new Date(a.split(" ")[0])) / 86400000);
}

function badge(d) {
  if (d.fare_is_main === "yes") return `<span class="badge yes">Main Cabin confirmed</span>`;
  if (d.fare_is_main === "no") return `<span class="badge no">Basic Economy</span>`;
  return `<span class="badge maybe" title="Google Explore's lowest price may be Basic Economy">may be Basic</span>`;
}

function renderPanel() {
  const d = S.city, q = S.query;
  let h = `<h2>${esc(d.city)} <span class="sub">${esc(d.code)}</span></h2>
    <div class="sub">${esc(d.country)} · ${esc(q.origin)} ⇄ ${esc(d.code)} · ${esc(q.depart)} – ${esc(q.ret)} · 1 adult, economy</div>
    <div class="fare">${money(d.price)} <small>lowest round trip</small>${badge(d)}</div>`;
  if (d.explore_price != null && d.explore_price !== d.price)
    h += `<div class="note">Explore's price was ${money(d.explore_price)} (Basic Economy); ${money(d.price)} is the lowest Main Cabin fare found.</div>`;
  if (S.out?.data && !S.out.data.domestic && d.fare_is_main !== "yes" && !S.finding)
    h += `<div class="links"><button id="findmain">Find Main Cabin fare (up to 2 searches)</button></div>
      <div class="note">Picks the cheapest outbound and return, then reads Delta's fare names (Main Basic / Main Classic…).</div>`;
  if (S.finding) h += `<div class="loading">Finding the Main Cabin fare…</div>`;
  if (S.out?.loading) h += `<div class="loading">Loading outbound flights…</div>`;
  if (S.out?.error) h += `<div class="error">${esc(S.out.error)}</div>`;
  if (S.out?.data) {
    const o = S.out.data;
    h += `<h3>Outbound · ${esc(q.origin)} → ${esc(d.code)} · ${esc(q.depart)} <span class="cost">${o.cached ? "from cache" : S.config.provider === "demo" ? "demo" : "used 1 search"}</span></h3>`;
    if (!o.domestic) h += `<div class="note">International: prices can include Basic fares. Pick both flights, then check the fare names (1 search).</div>`;
    else h += `<div class="note">US domestic: Basic Economy excluded, so these are Main Cabin or better.</div>`;
    h += itinTable(o.itineraries, "out", S.outSel);
  }
  if (S.ret?.loading) h += `<div class="loading">Loading return flights…</div>`;
  if (S.ret?.error) h += `<div class="error">${esc(S.ret.error)}</div>`;
  if (S.ret?.data) {
    const r = S.ret.data;
    h += `<h3>Return · ${esc(d.code)} → ${esc(q.origin)} · ${esc(q.ret)} <span class="cost">${r.cached ? "from cache" : S.config.provider === "demo" ? "demo" : "used 1 search"}</span></h3>
      <div class="note">Prices are the round-trip total with the chosen outbound flight.</div>`;
    h += itinTable(r.itineraries, "ret", S.retSel);
  }
  if (S.retSel != null) h += bookingBlock();
  $("#panelbody").innerHTML = h;
}

function itinTable(its, kind, sel) {
  if (!its.length) return `<div class="note">No Delta itineraries found.</div>`;
  const rows = its.map((it, i) => {
    const s = it.segments, a = s[0], z = s[s.length - 1], dd = dayDiff(a.depart, z.arrive);
    const via = it.layovers.map((l) => `${l.code} ${dur(l.minutes)}`).join(", ");
    const carriers = [...new Set(s.map((x) => x.airline))].join(", ");
    const op = s.filter((x) => x.operated_by).map((x) => x.operated_by).join("; ");
    return `<tr data-kind="${kind}" data-i="${i}" class="${sel === i ? "sel" : ""}">
      <td>${fmtTime(a.depart)}</td>
      <td>${fmtTime(z.arrive)}${dd > 0 ? `<span class="plus1">+${dd}</span>` : ""}</td>
      <td class="num">${dur(it.minutes)}</td>
      <td>${it.layovers.length ? `${it.layovers.length} · ${esc(via)}` : "Nonstop"}</td>
      <td class="fl">${esc(s.map((x) => x.flight_number).join(", "))}<br>${esc(carriers)}${op ? "<br>" + esc(op) : ""}</td>
      <td class="num"><b>${money(it.price)}</b></td></tr>`;
  }).join("");
  return `<table class="itins"><thead><tr><th>Depart</th><th>Arrive</th><th class="num">Duration</th><th>Stops via</th><th>Flights · carrier</th><th class="num">Price</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function bookingBlock() {
  const q = S.query, d = S.city;
  const url = S.ret?.data?.google_flights_url || S.out?.data?.google_flights_url;
  let h = `<div class="links">
    ${url ? `<a class="primary" href="${esc(url)}" target="_blank" rel="noopener">Open in Google Flights</a>` : ""}
    <a href="https://www.delta.com/flight-search/book-a-flight" target="_blank" rel="noopener">Book on delta.com</a>`;
  if (!S.out.data.domestic && !S.book) h += `<button id="checkfare">Check fare names (1 search)</button>`;
  h += `</div>`;
  if (S.config.provider === "demo") h += `<div class="note">Demo data: the Google Flights link is a generic search, not this itinerary.</div>`;
  else h += `<div class="note">delta.com opens its search page: enter ${esc(q.origin)} → ${esc(d.code)}, ${esc(q.depart)} – ${esc(q.ret)}.</div>`;
  if (S.book?.loading) h += `<div class="loading">Loading booking options…</div>`;
  if (S.book?.error) h += `<div class="error">${esc(S.book.error)}</div>`;
  if (S.book?.data) {
    const opts = S.book.data.options;
    h += `<h3>Fares for this itinerary <span class="cost">${S.book.data.cached ? "from cache" : ""}</span></h3>`;
    h += opts.length ? `<table><thead><tr><th>Sold by</th><th>Fare</th><th></th><th class="num">Price</th></tr></thead><tbody>${
      opts.map((o) => `<tr><td>${esc(o.book_with)}${o.leg !== "together" ? ` (${esc(o.leg)})` : ""}</td><td>${esc(o.fare_name || "—")}</td>
        <td>${o.fare_is_main === "yes" ? '<span class="badge yes">Main or better</span>' : o.fare_is_main === "no" ? '<span class="badge no">Basic</span>' : ""}</td>
        <td class="num">${money(o.price)}</td></tr>`).join("")}</tbody></table>` : `<div class="note">No booking options returned.</div>`;
  }
  return h;
}

function drawRoutes() {
  routeLayer.clearLayers();
  const coords = { ...(S.out?.data?.coords || {}), ...(S.ret?.data?.coords || {}) };
  const path = (it) => { const s = it.segments; return [s[0].from, ...s.map((x) => x.to)]; };
  const line = (codes, style) => {
    for (let i = 0; i < codes.length - 1; i++) {
      const a = coords[codes[i]], b = coords[codes[i + 1]];
      if (a && b) L.polyline(greatCircle(a, b), style).addTo(routeLayer);
    }
  };
  const outs = S.out?.data?.itineraries || [];
  const seen = new Set();
  outs.forEach((it, i) => {
    if (i === S.outSel) return;
    const k = path(it).join("-"); if (seen.has(k)) return; seen.add(k);
    line(path(it), { color: "#b5172b", weight: 1.5, opacity: 0.25 });
  });
  const sel = S.outSel != null ? outs[S.outSel] : outs[0];
  if (sel) line(path(sel), { color: "#b5172b", weight: 3.5, opacity: 0.95 });
  const r = S.retSel != null ? S.ret?.data?.itineraries[S.retSel] : null;
  if (r) line(path(r), { color: "#1d2433", weight: 2.5, opacity: 0.85, dashArray: "6 6" });
  // connection dots
  const hubs = new Set();
  [sel, r].filter(Boolean).forEach((it) => it.layovers.forEach((l) => hubs.add(l.code)));
  hubs.forEach((c) => coords[c] && L.circleMarker([coords[c][0], nearLon(coords[c][1])], { radius: 4, color: "#1d2433", weight: 1.5, fillColor: "#fff", fillOpacity: 1 })
    .bindTooltip(c, { className: "tip", permanent: false }).addTo(routeLayer));
}

async function openCity(code) {
  const d = S.dests.get(code); if (!d) return;
  S.city = d; S.out = { loading: true }; S.outSel = null; S.ret = null; S.retSel = null; S.book = null; S.finding = false;
  $("#panel").classList.add("open"); $("#panel").setAttribute("aria-hidden", "false");
  renderMap(); renderPanel(); routeLayer.clearLayers();
  const q = S.query;
  const r = await guarded(`/api/flights?origin=${q.origin}&dest=${code}&depart=${q.depart}&ret=${q.ret}`);
  if (S.city !== d) return;
  if (r.cancelled) { S.out = { error: "Cancelled." }; return renderPanel(); }
  if (r.error) { S.out = { error: r.error }; return renderPanel(); }
  S.out = { data: r.data };
  if (r.data.main_confirmed && r.data.itineraries.length) {
    const p = Math.min(...r.data.itineraries.map((it) => it.price));
    if (d.explore_price == null) d.explore_price = d.price;
    d.price = p; d.fare_is_main = "yes";
    renderMap();
  }
  renderPanel(); drawRoutes(); fitRoute(); refreshQuota();
}

function fitRoute() {
  const c = S.out?.data?.coords; if (!c) return;
  const pts = Object.values(c).map(([la, lo]) => [la, nearLon(lo)]);
  if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.25), { paddingBottomRight: [Math.min(720, innerWidth * 0.5), 0], maxZoom: 6 });
}

$("#panelbody").addEventListener("click", async (e) => {
  if (e.target.id === "checkfare") return checkFare();
  if (e.target.id === "findmain") return findMain();
  const tr = e.target.closest("tr[data-kind]"); if (!tr) return;
  const i = Number(tr.dataset.i);
  if (tr.dataset.kind === "out") await pickOut(i);
  else { S.retSel = i; S.book = null; renderPanel(); drawRoutes(); }
});

async function pickOut(i) {
  const q = S.query, d = S.city;
  if (S.outSel === i && S.ret?.data) return;
  S.outSel = i; S.ret = { loading: true }; S.retSel = null; S.book = null;
  renderPanel(); drawRoutes();
  const it = S.out.data.itineraries[i];
  if (!it.departure_token) { S.ret = { error: "This option has no return lookup token." }; return renderPanel(); }
  const r = await guarded(`/api/returns?origin=${q.origin}&dest=${d.code}&depart=${q.depart}&ret=${q.ret}&token=${encodeURIComponent(it.departure_token)}`);
  if (S.city !== d || S.outSel !== i) return;
  S.ret = r.data ? { data: r.data } : { error: r.error || "Cancelled." };
  renderPanel(); drawRoutes(); refreshQuota();
}

async function findMain() {
  const d = S.city;
  if (!S.out?.data?.itineraries.length) return;
  S.finding = true; renderPanel();
  try {
    await pickOut(0);                                   // cheapest outbound (lists are price-sorted)
    if (S.city !== d || !S.ret?.data?.itineraries.length) return;
    S.retSel = 0; S.book = null; drawRoutes();          // cheapest return
    await checkFare();
  } finally { S.finding = false; if (S.city === d) renderPanel(); }
}

async function checkFare() {
  const it = S.ret?.data?.itineraries[S.retSel], q = S.query, d = S.city;
  if (!it?.booking_token) { S.book = { error: "No booking token for this itinerary." }; return renderPanel(); }
  S.book = { loading: true }; renderPanel();
  const r = await guarded(`/api/booking?origin=${q.origin}&dest=${d.code}&depart=${q.depart}&ret=${q.ret}&token=${encodeURIComponent(it.booking_token)}`);
  S.book = r.data ? { data: r.data } : { error: r.error || "Cancelled." };
  const mp = r.data?.main_price;
  if (mp != null && S.city === d && (d.fare_is_main !== "yes" || mp < d.price)) {
    if (d.explore_price == null) d.explore_price = d.price;
    d.price = mp; d.fare_is_main = "yes"; renderMap();
  }
  renderPanel(); refreshQuota();
}

/* ---------------- airport autocomplete ---------------- */
let acItems = [], acHi = -1, acTimer = null;
function hideAC() { $("#ac").hidden = true; acItems = []; acHi = -1; }
function showAC() {
  const ul = $("#ac");
  if (!acItems.length) return hideAC();
  ul.innerHTML = acItems.map((a, i) => `<li data-i="${i}" class="${i === acHi ? "hi" : ""}"><b>${esc(a.code)}</b><span>${esc(a.city)} · ${esc(a.name)}</span></li>`).join("");
  ul.hidden = false;
}
function pickAC(i) { const a = acItems[i]; if (a) $("#origin").value = `${a.code} · ${a.city}`; hideAC(); }
$("#origin").addEventListener("input", (e) => {
  clearTimeout(acTimer);
  const q = e.target.value.trim(); if (q.length < 2) return hideAC();
  acTimer = setTimeout(async () => { acItems = (await getJSON(`/api/airports?q=${encodeURIComponent(q)}`)).body || []; acHi = -1; showAC(); }, 120);
});
$("#origin").addEventListener("keydown", (e) => {
  if ($("#ac").hidden) return;
  if (e.key === "ArrowDown") { acHi = Math.min(acItems.length - 1, acHi + 1); showAC(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { acHi = Math.max(0, acHi - 1); showAC(); e.preventDefault(); }
  else if (e.key === "Enter" && acHi >= 0) { pickAC(acHi); e.preventDefault(); }
  else if (e.key === "Escape") hideAC();
});
$("#ac").addEventListener("mousedown", (e) => { const li = e.target.closest("li"); if (li) { pickAC(Number(li.dataset.i)); e.preventDefault(); } });
$("#origin").addEventListener("blur", () => setTimeout(hideAC, 100));

/* ---------------- boot ---------------- */
(async function boot() {
  S.config = (await getJSON("/api/config")).body;
  const ids = S.config.regions.map((r) => r.id), saved = (localStorage.getItem("regions") || "").split(",").filter((r) => ids.includes(r));
  S.filter.regions = new Set(saved.length ? saved : ids);
  renderChips();
  const p = new URLSearchParams(location.search);
  const iso = (d) => d.toISOString().slice(0, 10);
  const t = new Date(); t.setDate(t.getDate() + 30);
  const r = new Date(t); r.setDate(r.getDate() + 7);
  $("#origin").value = p.get("origin") || S.config.default_origin;
  $("#depart").value = p.get("depart") || iso(t);
  $("#ret").value = p.get("ret") || iso(r);
  $("#depart").min = iso(new Date());
  $("#depart").addEventListener("change", () => { $("#ret").min = $("#depart").value; });
  const home = await getJSON(`/api/airport/${$("#origin").value.split(/\s/)[0]}`);
  if (home.ok) { S.home = home.body; map.setView([25, S.home.lon], 2); renderMap(); }
  refreshQuota(true); updateCost();
  setStatus(S.config.provider === "demo"
    ? "Demo mode: no live fares. Press Search to try the interface."
    : "Pick regions and dates, then Search. Each region is 1 search; repeats within 6 hours are free.");
})();
