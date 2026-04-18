// Phase 4 viewer. Pulls visible layers for the session, adds them to MapLibre,
// supports custom basemap (MapTiler/Mapbox key in localStorage), per-layer
// visibility toggles, click-to-inspect popups, and auto-refresh on session
// state changes (polls /api/<sid>/version every 2 s and re-syncs on diff).
const sessionId = location.pathname.split('/').pop();
const statusEl = document.getElementById('status');
const layersEl = document.getElementById('layers');
const LS_BASEMAP = 'geodata_basemap_style_url';
const POLL_MS = 2000;

const COLORS = ['#ff6b6b', '#4ecdc4', '#ffe66d', '#95e1d3', '#c7ceea',
                '#fcb1a6', '#a3d2ca', '#f6bd60', '#f28482', '#84a59d'];

function defaultStyle() {
  return {
    version: 8,
    sources: {
      'basemap': {
        type: 'raster',
        tiles: [
          'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
          'https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
          'https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
          'https://d.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
        ],
        tileSize: 256,
        attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © <a href="https://carto.com/attributions">CARTO</a>',
      },
    },
    layers: [
      { id: 'basemap', type: 'raster', source: 'basemap', minzoom: 0, maxzoom: 22 },
    ],
    glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
  };
}

const customStyle = localStorage.getItem(LS_BASEMAP);
const map = new maplibregl.Map({
  container: 'map',
  style: customStyle || defaultStyle(),
  center: [18.07, 59.33],
  zoom: 11,
});

map.addControl(new maplibregl.NavigationControl(), 'top-right');
map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');

map.on('load', async () => {
  await syncSession(true);
  setInterval(pollForChanges, POLL_MS);
});
map.on('error', (e) => console.warn('maplibre error', e.error?.message || e));

const basemapInput = document.getElementById('basemap-input');
const basemapApply = document.getElementById('basemap-apply');
const basemapReset = document.getElementById('basemap-reset');
basemapInput.value = customStyle || '';
basemapApply.addEventListener('click', () => {
  const val = basemapInput.value.trim();
  if (val) localStorage.setItem(LS_BASEMAP, val);
  else localStorage.removeItem(LS_BASEMAP);
  location.reload();
});
basemapReset.addEventListener('click', () => {
  localStorage.removeItem(LS_BASEMAP);
  basemapInput.value = '';
  location.reload();
});

// -- session → layers --

// name → { color, meta, layerIds: [...], data: FeatureCollection, areaEst: number }
const dataLayers = {};
let lastVersion = -1;
let autoFit = true;    // fit bounds on first sync; subsequent refreshes preserve view

async function pollForChanges() {
  try {
    const res = await fetch(`/api/${sessionId}/version`);
    if (!res.ok) return;
    const payload = await res.json();
    if (payload.version !== lastVersion) await syncSession(false);
  } catch (e) {
    // Silent — viewer stays showing last-good state on transient network errors.
  }
}

async function syncSession(isFirstLoad) {
  let payload;
  try {
    const res = await fetch(`/api/${sessionId}/visible_layers`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    payload = await res.json();
  } catch (e) {
    statusEl.textContent = `Failed to load session: ${e.message}`;
    return;
  }
  lastVersion = payload.version ?? 0;

  // Diff: remove layers that are no longer visible.
  const visible = new Set(payload.visible_layers || []);
  for (const name of Object.keys(dataLayers)) {
    if (!visible.has(name)) removeLayer(name);
  }
  if (!visible.size) {
    statusEl.textContent = 'No layers marked visible. Call show([...]) from the MCP tool.';
    layersEl.innerHTML = '';
    return;
  }

  statusEl.textContent =
    `Session ${payload.session_id.slice(0,10)}… — ${payload.visible_layers.length} layer(s) · v${payload.version}`;
  layersEl.innerHTML = '';

  let tightest = null;
  let i = 0;
  for (const name of payload.visible_layers) {
    const meta = payload.layers[name];
    const color = dataLayers[name]?.color || COLORS[i++ % COLORS.length];
    try {
      const fc = await (await fetch(`/api/${sessionId}/layer/${encodeURIComponent(name)}/geojson`)).json();
      // If we already had this layer, swap in new data; else add.
      if (dataLayers[name]) {
        map.getSource(`src-${name}`).setData(fc);
        dataLayers[name].data = fc;
        dataLayers[name].meta = meta;
      } else {
        const layerIds = addLayer(name, fc, color);
        dataLayers[name] = { color, meta, layerIds, data: fc,
                             areaEst: estimateAreaPriority(fc) };
      }
      // Re-sort layer draw order: smallest-area on top so they receive clicks.
      reorderLayers();
      const b = featureCollectionBounds(fc);
      if (b) {
        const area = (b[1][0] - b[0][0]) * (b[1][1] - b[0][1]);
        if (tightest == null || area < tightest.area) tightest = { b, area };
      }
    } catch (e) {
      console.error('layer load failed', name, e);
    }
    renderLayerRow(name, color, meta);
  }
  if (isFirstLoad && tightest) {
    map.fitBounds(tightest.b, { padding: 60, duration: 600 });
  }
  if (isFirstLoad) map.on('click', onMapClick);
}

function removeLayer(name) {
  const info = dataLayers[name];
  if (!info) return;
  for (const id of info.layerIds) {
    if (map.getLayer(id)) map.removeLayer(id);
  }
  if (map.getSource(`src-${name}`)) map.removeSource(`src-${name}`);
  delete dataLayers[name];
}

function addLayer(name, fc, color) {
  const sourceId = `src-${name}`;
  map.addSource(sourceId, { type: 'geojson', data: fc });
  const ids = [];
  map.addLayer({ id: `${name}-fill`, type: 'fill', source: sourceId,
    paint: { 'fill-color': color, 'fill-opacity': 0.12 },
    filter: ['==', '$type', 'Polygon'] });
  ids.push(`${name}-fill`);
  map.addLayer({ id: `${name}-outline`, type: 'line', source: sourceId,
    paint: { 'line-color': color, 'line-width': 1.2, 'line-opacity': 0.9 },
    filter: ['==', '$type', 'Polygon'] });
  ids.push(`${name}-outline`);
  map.addLayer({ id: `${name}-line`, type: 'line', source: sourceId,
    paint: { 'line-color': color, 'line-width': 1.6 },
    filter: ['==', '$type', 'LineString'] });
  ids.push(`${name}-line`);
  map.addLayer({ id: `${name}-pt`, type: 'circle', source: sourceId,
    paint: { 'circle-color': color, 'circle-radius': 3.5,
             'circle-stroke-color': '#fff', 'circle-stroke-width': 0.6 },
    filter: ['==', '$type', 'Point'] });
  ids.push(`${name}-pt`);
  return ids;
}

// Sort drawn data-layers: largest (background polygons) at the bottom,
// smallest (points / buildings) on top. Uses rough bbox area as proxy.
function reorderLayers() {
  const names = Object.keys(dataLayers);
  names.sort((a, b) => (dataLayers[b].areaEst ?? 0) - (dataLayers[a].areaEst ?? 0));
  // MapLibre draws layers in the order they're added; moveLayer to bring to top.
  for (const n of names) {
    for (const id of dataLayers[n].layerIds) {
      if (map.getLayer(id)) map.moveLayer(id);
    }
  }
}

function estimateAreaPriority(fc) {
  // Rough bbox-area estimate. Polygons covering big areas sort "bigger".
  const b = featureCollectionBounds(fc);
  if (!b) return 0;
  return (b[1][0] - b[0][0]) * (b[1][1] - b[0][1]);
}

function renderLayerRow(name, color, meta) {
  const el = document.createElement('div');
  el.className = 'layer-row';
  el.innerHTML = `
    <input type="checkbox" id="cb-${name}" checked data-layer="${name}"/>
    <label for="cb-${name}">
      <div><strong style="color:${color}">${escapeHtml(name)}</strong></div>
      <div class="meta">${meta.geometry_type ?? '(no geometry)'} · ${meta.feature_count} features${meta.notes ? ` · <em>${escapeHtml(meta.notes.slice(0,60))}${meta.notes.length>60?'…':''}</em>` : ''}</div>
    </label>`;
  const cb = el.querySelector('input');
  cb.addEventListener('change', (e) => toggleLayer(name, e.target.checked));
  layersEl.appendChild(el);
}

function toggleLayer(name, visible) {
  const info = dataLayers[name];
  if (!info) return;
  for (const id of info.layerIds) {
    map.setLayoutProperty(id, 'visibility', visible ? 'visible' : 'none');
  }
}

// -- click-to-inspect popup --

let currentPopup = null;

function onMapClick(e) {
  const allIds = [];
  for (const info of Object.values(dataLayers)) allIds.push(...info.layerIds);
  const feats = map.queryRenderedFeatures(e.point, { layers: allIds });
  if (!feats.length) return;

  // Pick the feature from the "smallest" layer at this point — reduces the
  // classic polygon-masks-building case where a borough polygon wins over a
  // specific building. "Smallest" means the layer whose estimated bbox
  // priority is lowest (buildings < districts < boroughs).
  const byLayer = new Map();  // layerName → feature (first hit per layer)
  for (const f of feats) {
    const srcId = f.source;
    const layerName = srcId.startsWith('src-') ? srcId.slice(4) : srcId;
    if (!byLayer.has(layerName)) byLayer.set(layerName, f);
  }
  const ranked = [...byLayer.entries()].sort(
    (a, b) => (dataLayers[a[0]]?.areaEst ?? 0) - (dataLayers[b[0]]?.areaEst ?? 0)
  );
  const [topName, topFeat] = ranked[0];
  const other = ranked.slice(1).map(([n]) => n);

  const color = dataLayers[topName]?.color || '#6cf';
  const props = topFeat.properties || {};
  const rows = Object.entries(props)
    .filter(([k, v]) => v !== null && v !== undefined && v !== '')
    .slice(0, 12)
    .map(([k, v]) => `<div class="row"><span class="k">${escapeHtml(k)}</span><span class="v">${escapeHtml(String(v))}</span></div>`)
    .join('');
  const otherHtml = other.length
    ? `<div class="meta" style="margin-top:6px;opacity:0.7">Also at this point: ${escapeHtml(other.join(', '))}</div>`
    : '';
  const html = `
    <div class="feature-popup">
      <div style="color:${color};font-weight:600;margin-bottom:4px">${escapeHtml(topName)}</div>
      ${rows || '<div class="meta">(no non-empty properties)</div>'}
      ${otherHtml}
    </div>`;
  if (currentPopup) currentPopup.remove();
  currentPopup = new maplibregl.Popup({ closeOnClick: true, maxWidth: '320px' })
    .setLngLat(e.lngLat)
    .setHTML(html)
    .addTo(map);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// -- bounds helpers --

function featureCollectionBounds(fc) {
  let xmin = Infinity, ymin = Infinity, xmax = -Infinity, ymax = -Infinity;
  let any = false;
  for (const f of fc.features ?? []) walk(f.geometry);
  function walk(g) {
    if (!g) return;
    if (g.type === 'GeometryCollection') return g.geometries.forEach(walk);
    walkCoords(g.coordinates);
  }
  function walkCoords(c) {
    if (typeof c[0] === 'number') {
      const [x, y] = c;
      if (x < xmin) xmin = x; if (x > xmax) xmax = x;
      if (y < ymin) ymin = y; if (y > ymax) ymax = y;
      any = true;
    } else for (const cc of c) walkCoords(cc);
  }
  return any ? [[xmin, ymin], [xmax, ymax]] : null;
}
