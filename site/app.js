/* Field mushroom map — client. Reads data/cells.json, data/scores.json and
   data/pasture/<chunk>.json, all produced by the GitHub Actions workflows. */
(() => {
  'use strict';

  // Orange single-hue ramp, light -> dark, validated against the grey base map.
  const RAMP = ['#e98937', '#d55d0d', '#b53700', '#861f12'];
  const RAMP_STROKE = ['#d55d0d', '#b53700', '#861f12', '#5c150c'];
  const LABELS = ['Low', 'Fair', 'Good', 'Very good'];
  const ZERO = { fill: '#c4c4bf', stroke: '#6f6f6b', label: 'Not suitable' };
  const NONE = { fill: '#bdbdb9', stroke: '#8a8a86', label: 'No score' };
  const PASTURE_MIN_ZOOM = 12;
  const TZ = 'Europe/Stockholm';

  const $ = (id) => document.getElementById(id);
  const state = {
    cells: null, scores: null, grid: null, q: 1e5,
    cellRows: new Map(), day: 0, todayIdx: -1,
    chunks: new Map(), cellLayer: null, pastureGroup: null, locateLayer: null,
  };

  // ---------- helpers ----------
  const fmtDay = new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
  const fmtInitial = new Intl.DateTimeFormat('en-GB', { weekday: 'narrow', timeZone: 'UTC' });
  const fmtTime = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', day: 'numeric', month: 'short', timeZone: TZ });
  const todayISO = () => new Intl.DateTimeFormat('sv-SE', { timeZone: TZ }).format(new Date());
  const asDate = (iso) => new Date(iso + 'T12:00:00Z');
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function bin(s) {
    if (s == null) return -2;
    if (s <= 0) return -1;
    return s >= 75 ? 3 : s >= 50 ? 2 : s >= 25 ? 1 : 0;
  }
  function look(s) {
    const b = bin(s);
    if (b === -2) return NONE;
    if (b === -1) return ZERO;
    return { fill: RAMP[b], stroke: RAMP_STROKE[b], label: LABELS[b] };
  }
  function cellData(cid, day) {
    const c = state.scores && state.scores.cells && state.scores.cells[cid];
    if (!c) return { s: null, r: null, t: null, f: null };
    return { s: c[0][day], r: c[1][day], t: c[2][day], f: c[3][day] };
  }
  function dayName(idx) {
    const d = state.scores.dates[idx];
    const k = idx - state.todayIdx;
    const base = fmtDay.format(asDate(d));
    if (state.todayIdx >= 0 && k === 0) return 'Today · ' + base;
    if (state.todayIdx >= 0 && k === 1) return 'Tomorrow · ' + base;
    return base;
  }
  function dayNote(idx) {
    const n = (state.scores.model && state.scores.model.window_days) || 5;
    if (!state.scores.inSeason[idx]) return 'Out of season (scores July–November only)';
    if (state.todayIdx < 0) return '';
    const k = idx - state.todayIdx;
    const past = Math.max(0, n - 1 - k);
    if (past > 0) return `${past} past day${past > 1 ? 's' : ''} + ${n - past} forecast day${n - past > 1 ? 's' : ''}`;
    if (k >= 6) return 'Forecast only — rain this far ahead is uncertain';
    return 'Forecast only';
  }
  function cellBounds(cid) {
    const [i, j] = cid.split('_').map(Number);
    const g = state.grid;
    const s = g.origin_lat + i * g.dlat, w = g.origin_lon + j * g.dlon;
    return L.latLngBounds([s, w], [s + g.dlat, w + g.dlon]);
  }
  function mapsLink(lat, lon) {
    const ios = /iPad|iPhone|iPod/.test(navigator.userAgent);
    const ll = `${lat.toFixed(5)},${lon.toFixed(5)}`;
    return ios ? `https://maps.apple.com/?q=${ll}` : `https://www.google.com/maps/search/?api=1&query=${ll}`;
  }
  async function getJSON(url) {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) throw new Error(`${r.status} ${url}`);
    return r.json();
  }

  // ---------- map ----------
  const map = L.map('map', { preferCanvas: true, minZoom: 6, maxZoom: 18, zoomSnap: 0.5 });
  const renderer = L.canvas({ padding: 0.4, tolerance: 6 });
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, className: 'basemap',
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  map.attributionControl.setPrefix(false);
  map.attributionControl.addAttribution('Weather <a href="https://open-meteo.com/">Open-Meteo.com</a> (CC BY 4.0)');
  map.attributionControl.addAttribution('Pasture © <a href="https://jordbruksverket.se/">Jordbruksverket</a>');
  map.setView([55.95, 13.55], 8);

  // ---------- weather cells (overview) ----------
  function cellStyle(cid) {
    const { s } = cellData(cid, state.day);
    const lk = look(s);
    return { renderer, color: '#ffffff', weight: 1, opacity: 0.9, fillColor: lk.fill,
      fillOpacity: s == null ? 0.45 : 0.85, dashArray: null };
  }
  function buildCells() {
    const group = L.layerGroup();
    for (const row of state.cells.cells) {
      const cid = row[0];
      state.cellRows.set(cid, row);
      const rect = L.rectangle(cellBounds(cid), cellStyle(cid));
      rect.cid = cid;
      rect.on('click', (e) => openCellPopup(cid, e.latlng));
      group.addLayer(rect);
    }
    state.cellLayer = group;
  }

  // ---------- pastures (zoomed in) ----------
  function pastureStyle(cid) {
    const { s } = cellData(cid, state.day);
    const lk = look(s);
    return { renderer, color: lk.stroke, weight: 1.5, opacity: 1, fillColor: lk.fill,
      fillOpacity: s == null ? 0.3 : 0.85, dashArray: s == null ? '3 3' : null };
  }
  function decodeRing(r, q) {
    let x = r[0], y = r[1];
    const pts = [[y / q, x / q]];
    for (let k = 2; k < r.length; k += 2) { x += r[k]; y += r[k + 1]; pts.push([y / q, x / q]); }
    return pts;
  }
  async function loadChunk(id) {
    let entry = state.chunks.get(id);
    if (entry) return entry;
    entry = { group: L.layerGroup(), layers: [], ready: null };
    state.chunks.set(id, entry);
    entry.ready = (async () => {
      const r = await fetch(`data/pasture/${id}.json?v=${encodeURIComponent(state.cells.version)}`);
      if (!r.ok) throw new Error(`chunk ${id}: ${r.status}`);
      const data = await r.json();
      for (const [bid, cid, a100, polys] of data.f) {
        const latlngs = polys.map((rings) => rings.map((ring) => decodeRing(ring, state.q)));
        const layer = L.polygon(latlngs, pastureStyle(cid));
        layer.info = { bid, cid, ha: a100 / 100 };
        layer.on('click', (e) => openPasturePopup(layer, e.latlng));
        entry.layers.push(layer);
        entry.group.addLayer(layer);
      }
    })().catch((err) => { state.chunks.delete(id); console.error(err); });
    return entry;
  }
  function refreshPastures() {
    if (!state.cells) return;
    const zoomedIn = map.getZoom() >= PASTURE_MIN_ZOOM;
    $('zoom-hint').hidden = zoomedIn || map.getZoom() < 10;
    if (!zoomedIn) {
      if (state.pastureGroup && map.hasLayer(state.pastureGroup)) map.removeLayer(state.pastureGroup);
      if (state.cellLayer && !map.hasLayer(state.cellLayer)) map.addLayer(state.cellLayer);
      return;
    }
    if (state.cellLayer && map.hasLayer(state.cellLayer)) map.removeLayer(state.cellLayer);
    if (!map.hasLayer(state.pastureGroup)) map.addLayer(state.pastureGroup);
    const view = map.getBounds().pad(0.25);
    for (const [id, b] of Object.entries(state.cells.chunks)) {
      const visible = view.intersects(L.latLngBounds([b[0], b[1]], [b[2], b[3]]));
      const entry = state.chunks.get(id);
      if (visible) {
        loadChunk(id).then((e) => e.ready).then(() => {
          const en = state.chunks.get(id);
          if (en && !state.pastureGroup.hasLayer(en.group)) {
            en.layers.forEach((l) => l.setStyle(pastureStyle(l.info.cid)));
            state.pastureGroup.addLayer(en.group);
          }
        });
      } else if (entry && state.pastureGroup.hasLayer(entry.group)) {
        state.pastureGroup.removeLayer(entry.group);
      }
    }
  }

  // ---------- popups ----------
  function weatherTable(cid) {
    const { s, r, t, f } = cellData(cid, state.day);
    const lk = look(s);
    const score = s == null ? '—' : `${s}%`;
    const fmt = (v, unit) => (v == null ? '—' : `${v} ${unit}`);
    return `<div class="pop-title"><i class="sw" style="--c:${lk.fill};--s:${lk.stroke}"></i>${esc(lk.label)} · ${score}</div>
      <div class="pop-date">${esc(dayName(state.day))}</div>
      <table>
        <tr><td>Rain, 5 days</td><td>${fmt(r, 'mm')}</td></tr>
        <tr><td>Mean temperature</td><td>${fmt(t, '°C')}</td></tr>
        <tr><td>Frost nights</td><td>${f == null ? '—' : f}</td></tr>
      </table>`;
  }
  function openPasturePopup(layer, latlng) {
    const c = layer.getBounds().getCenter();
    const html = `<div class="pop">${weatherTable(layer.info.cid)}
      <div class="foot">Pasture block ${esc(layer.info.bid)} · ${layer.info.ha.toFixed(1)} ha</div>
      <div class="links"><a href="${mapsLink(c.lat, c.lng)}" target="_blank" rel="noopener">Open in maps</a></div></div>`;
    L.popup({ maxWidth: 280 }).setLatLng(latlng).setContent(html).openOn(map);
  }
  function openCellPopup(cid, latlng) {
    const row = state.cellRows.get(cid);
    const html = `<div class="pop">${weatherTable(cid)}
      <div class="foot">Weather square (~5 km) with ${row[4]} pasture block${row[4] === 1 ? '' : 's'}, ${Math.round(row[3])} ha</div>
      <div class="links"><button class="linklike" type="button" data-zoom="${cid}">Zoom in to pastures</button></div></div>`;
    const p = L.popup({ maxWidth: 280 }).setLatLng(latlng).setContent(html).openOn(map);
    const btn = p.getElement().querySelector('[data-zoom]');
    btn.addEventListener('click', () => { map.closePopup(); map.flyTo(cellBounds(cid).getCenter(), 13); });
  }

  // ---------- day selection ----------
  function setDay(idx) {
    const sc = state.scores;
    const min = Number($('day').min), max = Number($('day').max);
    idx = Math.max(min, Math.min(max, idx));
    state.day = idx;
    $('day').value = String(idx);
    $('day').setAttribute('aria-valuetext', dayName(idx));
    $('day-name').textContent = dayName(idx);
    $('day-note').textContent = dayNote(idx);
    $('prev').disabled = idx <= min;
    $('next').disabled = idx >= max;
    document.querySelectorAll('#ticks span').forEach((el) => el.classList.toggle('sel', Number(el.dataset.i) === idx));
    const banner = $('banner');
    if (sc.status === 'out-of-season' || !sc.inSeason[idx]) {
      banner.textContent = 'Out of season: the map scores July–November only. Pastures are shown without a score.';
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
    if (state.cellLayer) state.cellLayer.eachLayer((l) => l.setStyle(cellStyle(l.cid)));
    for (const entry of state.chunks.values()) entry.layers.forEach((l) => l.setStyle(pastureStyle(l.info.cid)));
    map.closePopup();
  }
  function buildTicks(min, max) {
    const t = $('ticks');
    t.innerHTML = '';
    for (let i = min; i <= max; i++) {
      const s = document.createElement('span');
      s.dataset.i = String(i);
      s.textContent = fmtInitial.format(asDate(state.scores.dates[i]));
      t.appendChild(s);
    }
  }

  // ---------- about dialog ----------
  function fillAbout() {
    const m = (state.scores && state.scores.model) || null;
    const list = $('model-list');
    list.innerHTML = '';
    if (m) {
      const n = m.window_days;
      const items = [
        `<b>Rain:</b> total over ${n} days. 0 at ${m.rain_mm[0]} mm or less, rising evenly to full score at ${m.rain_mm[1]} mm.`,
        `<b>Temperature:</b> ${n}-day mean of daily mean temperature. Full score ${m.temp_c[1]}–${m.temp_c[2]} °C, falling to 0 at ${m.temp_c[0]} °C and at ${m.temp_c[3]} °C; 0 above ${m.temp_c[3]} °C.`,
        `<b>Frost:</b> each night below ${m.frost_below_c} °C subtracts ${Math.round(m.frost_penalty * 100)} points.`,
        `<b>Score</b> = rain × temperature − frost, from 0 to 100.`,
        `<b>Season:</b> scored in ${m.season_months.length === 5 && m.season_months[0] === 7 ? 'July–November' : 'months ' + m.season_months.join(', ')} only.`,
        `<b>Land:</b> permanent pasture (betesmark) only.`,
      ];
      items.forEach((h) => { const li = document.createElement('li'); li.innerHTML = h; list.appendChild(li); });
    }
    const dl = $('data-list');
    dl.innerHTML = '';
    const rows = [];
    if (state.scores && state.scores.generated) {
      rows.push(`Weather: <a href="https://open-meteo.com/" rel="noopener">Open-Meteo.com</a>, CC BY 4.0. Updated ${esc(fmtTime.format(new Date(state.scores.generated)))}. The score is derived from it.`);
    }
    if (state.cells && state.cells.source) {
      const s = state.cells.source;
      rows.push(`Pasture: <a href="${esc(s.url)}" rel="noopener">Jordbruksverket</a>, farm blocks ${esc(s.year)} (${esc(s.types.join(', '))}), ${state.cells.n_blocks.toLocaleString('en-GB')} blocks in ${esc(s.region)}.`);
    }
    rows.push('Base map: © <a href="https://www.openstreetmap.org/copyright" rel="noopener">OpenStreetMap</a> contributors.');
    rows.forEach((h) => { const li = document.createElement('li'); li.innerHTML = h; dl.appendChild(li); });
  }

  // ---------- location ----------
  function locate() {
    $('locate').classList.add('active');
    map.locate({ setView: true, maxZoom: 14, enableHighAccuracy: true });
  }
  map.on('locationfound', (e) => {
    if (state.locateLayer) map.removeLayer(state.locateLayer);
    state.locateLayer = L.layerGroup([
      L.circle(e.latlng, { radius: e.accuracy, renderer, color: '#1c5cab', weight: 1, fillOpacity: 0.08, interactive: false }),
      L.circleMarker(e.latlng, { radius: 7, renderer, color: '#ffffff', weight: 2.5, fillColor: '#1c5cab', fillOpacity: 1, interactive: false }),
    ]).addTo(map);
  });
  map.on('locationerror', (e) => {
    $('locate').classList.remove('active');
    $('status').textContent = 'Could not get your location: ' + e.message;
  });

  // ---------- startup ----------
  async function start() {
    $('locate').addEventListener('click', locate);
    $('info').addEventListener('click', () => { fillAbout(); $('about').showModal(); });
    $('prev').addEventListener('click', () => setDay(state.day - 1));
    $('next').addEventListener('click', () => setDay(state.day + 1));
    $('day').addEventListener('input', (e) => setDay(Number(e.target.value)));
    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || $('about').open || !state.scores) return;
      if (e.key === 'ArrowLeft') setDay(state.day - 1);
      if (e.key === 'ArrowRight') setDay(state.day + 1);
    });

    const [cellsRes, scoresRes] = await Promise.allSettled([getJSON('data/cells.json'), getJSON('data/scores.json')]);
    const status = $('status');
    if (cellsRes.status !== 'fulfilled' || !cellsRes.value.cells || !cellsRes.value.cells.length) {
      $('day-name').textContent = 'No pasture data yet';
      status.textContent = 'The pasture layer has not been built yet. It is created by the "Build pasture layer" workflow.';
      return;
    }
    state.cells = cellsRes.value;
    state.grid = state.cells.grid;
    state.q = state.cells.q || 1e5;
    if (state.cells.source && state.cells.source.region) $('region').textContent = state.cells.source.region;

    state.scores = scoresRes.status === 'fulfilled' ? scoresRes.value : null;
    if (!state.scores || !state.scores.dates) {
      state.scores = { dates: [todayISO()], inSeason: [false], cells: {}, status: 'missing' };
    }
    const sc = state.scores;
    state.todayIdx = sc.dates.indexOf(todayISO());
    const min = state.todayIdx >= 0 ? state.todayIdx : 0;
    const max = sc.dates.length - 1;
    $('day').min = String(min);
    $('day').max = String(max);
    buildTicks(min, max);

    state.pastureGroup = L.layerGroup();
    buildCells();
    map.fitBounds(L.latLngBounds(state.cells.cells.map((r) => cellBounds(r[0]).getCenter())).pad(0.05));
    map.on('zoomend moveend', refreshPastures);
    refreshPastures();
    setDay(min);

    const parts = [];
    if (sc.generated) parts.push('Weather updated ' + fmtTime.format(new Date(sc.generated)));
    if (sc.status === 'missing') parts.push('Weather data not available yet');
    if (state.todayIdx < 0 && sc.dates.length && sc.dates[sc.dates.length - 1] < todayISO()) {
      parts.push('Weather data is out of date');
    } else if (state.todayIdx < 0 && sc.status === 'ok') {
      parts.push('Weather not updated today yet');
    }
    if (state.cells.source) parts.push(`pasture ${state.cells.source.year}`);
    status.textContent = parts.join(' · ');
  }

  start().catch((err) => {
    console.error(err);
    $('status').textContent = 'Something went wrong loading the map: ' + err.message;
  });

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => navigator.serviceWorker.register('sw.js').catch(() => {}));
  }
})();
