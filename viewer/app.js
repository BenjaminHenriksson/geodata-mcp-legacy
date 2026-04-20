// Phase 4 viewer. Pulls visible layers for the session, adds them to MapLibre,
// supports custom basemap (MapTiler/Mapbox key in localStorage), per-layer
// visibility toggles, click-to-inspect popups, and auto-refresh on session
// state changes (polls /api/<sid>/version every 2 s and re-syncs on diff).
const sessionId = location.pathname.split('/').pop();
const statusEl = document.getElementById('status');
const layersEl = document.getElementById('layers');
const titleEl = document.getElementById('viewer-title');
const DEFAULT_TITLE = 'Geodata viewer';
const LS_BASEMAP = 'geodata_basemap_style_url';
const POLL_MS = 2000;

// Cartographer's-ink palette: iron gall, Van Dyke brown, burnt sienna,
// verdigris, raw umber. All desaturated, all warm-biased, all at a similar
// value so no single layer dominates. Reads as annotations on paper rather
// than dashboard accents.
// Zoom-responsive point radius. Keeps points readable without having them
// merge into a city-wide blob at low zoom or becoming a single pixel at
// high zoom. Used as the fallback for the size channel in applyStyle.
const DEFAULT_POINT_RADIUS_EXPR = [
  'interpolate', ['linear'], ['zoom'],
  8,  1.0,
  11, 2.2,
  13, 3.0,
  15, 3.8,
  18, 6.0,
];

const COLORS = ['#B05B3B',  // terra (primary accent, shared with site)
                '#7A4A35',  // burnt umber
                '#8C6A3E',  // raw sienna / ochre
                '#5C5040',  // sepia ink
                '#6B7348',  // olive / moss
                '#4F5E6B',  // payne's grey, warm
                '#8A5A7A',  // muted plum
                '#4E7F7A',  // verdigris
                '#A5462F',  // vermillion
                '#9A7A42']; // antique brass

function defaultStyle() {
  return {
    version: 8,
    sources: {
      'basemap': {
        type: 'raster',
        tiles: [
          'https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png',
          'https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png',
          'https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png',
          'https://d.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png',
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

  // Title: server-set via show(title=...), falls back to default.
  const desiredTitle = payload.title || DEFAULT_TITLE;
  if (titleEl.textContent !== desiredTitle) titleEl.textContent = desiredTitle;
  document.title = payload.title
    ? `${payload.title} · Geodata viewer`
    : 'Geodata viewer';

  // Diff: remove layers that are no longer visible.
  const visible = new Set(payload.visible_layers || []);
  for (const name of Object.keys(dataLayers)) {
    if (!visible.has(name)) removeLayer(name);
  }
  if (!visible.size) {
    statusEl.textContent = 'No layers visible. Call show([...]) from the MCP tool.';
    layersEl.innerHTML = '';
    return;
  }

  const n = payload.visible_layers.length;
  statusEl.textContent =
    `${n} layer${n === 1 ? '' : 's'} · v${payload.version} · ${payload.session_id.slice(0,10)}`;
  layersEl.innerHTML = '';

  const styles = payload.styles || {};
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
      // Apply any per-layer thematic styling (set via show(..., style=...)).
      applyStyle(name, styles[name], color);
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
    renderLayerRow(name, color, meta, styles[name]);
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
    paint: { 'fill-color': color, 'fill-opacity': 0.22 },
    filter: ['==', '$type', 'Polygon'] });
  ids.push(`${name}-fill`);
  map.addLayer({ id: `${name}-outline`, type: 'line', source: sourceId,
    paint: { 'line-color': color, 'line-width': 1.3, 'line-opacity': 0.95 },
    filter: ['==', '$type', 'Polygon'] });
  ids.push(`${name}-outline`);
  map.addLayer({ id: `${name}-line`, type: 'line', source: sourceId,
    paint: { 'line-color': color, 'line-width': 1.8 },
    filter: ['==', '$type', 'LineString'] });
  ids.push(`${name}-line`);
  map.addLayer({ id: `${name}-pt`, type: 'circle', source: sourceId,
    paint: { 'circle-color': color,
             // Zoom-interp so points shrink when zoomed out instead of
             // covering the whole city as overlapping blobs.
             'circle-radius': DEFAULT_POINT_RADIUS_EXPR,
             'circle-stroke-color': '#F5F0E8', 'circle-stroke-width': 1.0 },
    filter: ['==', '$type', 'Point'] });
  ids.push(`${name}-pt`);
  return ids;
}

// Draw order: polygon fills at the bottom (largest bbox first), then polygon
// outlines, then lines, then points on top. This guarantees that e.g. a
// city-wide address point layer stays above a Södermalm polygon's fill, so
// the point is both visible and clickable through queryRenderedFeatures.
function reorderLayers() {
  const names = Object.keys(dataLayers);
  // Within each sublayer tier, draw larger-bbox layers first so smaller ones
  // sit on top of same-kind neighbours.
  const byArea = [...names].sort(
    (a, b) => (dataLayers[b].areaEst ?? 0) - (dataLayers[a].areaEst ?? 0)
  );
  const tiers = [
    (n) => `${n}-fill`,
    (n) => `${n}-outline`,
    (n) => `${n}-line`,
    (n) => `${n}-pt`,
  ];
  for (const makeId of tiers) {
    for (const n of byArea) {
      const id = makeId(n);
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

// Translate a server-side style spec into MapLibre paint expressions and
// apply them. Supports four channels:
//   color   — spec.column/scale/palette (per-value fill/line/circle color)
//   opacity — spec.opacity   = {column, range:[lo,hi]} (linear)
//   size    — spec.size      = {column, range:[lo,hi]} (circle-radius only)
//   stroke  — spec.stroke    = {column, range:[lo,hi]} (outline/line/point stroke width)
// Mutates spec.palette in place when scale==='categorical' and no palette
// was provided, so the legend reads back the auto-assigned mapping.
function applyStyle(name, spec, defaultColor) {
  const info = dataLayers[name];
  if (!info) return;
  const fill = `${name}-fill`;
  const outline = `${name}-outline`;
  const line = `${name}-line`;
  const pt = `${name}-pt`;

  const feats = info.data?.features || [];

  // --- COLOR ---
  let colorExpr = defaultColor;
  if (spec && spec.column) {
    if (spec.scale === 'linear' && Array.isArray(spec.palette) && spec.palette.length >= 2) {
      const range = numericRange(feats, spec.column);
      if (range) {
        const [lo, hi] = range;
        colorExpr = ['interpolate', ['linear'], ['to-number', ['get', spec.column]],
                     lo, spec.palette[0], hi, spec.palette[spec.palette.length - 1]];
      }
    } else if (spec.scale === 'categorical' || spec.scale == null) {
      // Auto-assign a categorical palette when none was supplied.
      // Mutates spec.palette so the legend reflects the assignment.
      if (!spec.palette || typeof spec.palette !== 'object' || Array.isArray(spec.palette)) {
        const values = distinctValues(feats, spec.column);
        const palette = {};
        values.forEach((v, i) => { palette[String(v)] = COLORS[i % COLORS.length]; });
        spec.palette = palette;
      }
      const pairs = [];
      for (const [value, color] of Object.entries(spec.palette)) {
        pairs.push(value, color);
      }
      if (pairs.length) {
        colorExpr = ['match', ['to-string', ['get', spec.column]], ...pairs, defaultColor];
      }
    }
  }

  setPaint(fill, 'fill-color', colorExpr);
  setPaint(outline, 'line-color', colorExpr);
  setPaint(line, 'line-color', colorExpr);
  setPaint(pt, 'circle-color', colorExpr);

  // --- OPACITY ---
  const opacityFill = channelExpr(spec?.opacity, feats, 0.22);
  const opacityLine = channelExpr(spec?.opacity, feats, 0.95);
  const opacityPt = channelExpr(spec?.opacity, feats, 1.0);
  setPaint(fill, 'fill-opacity', opacityFill);
  setPaint(outline, 'line-opacity', opacityLine);
  setPaint(line, 'line-opacity', opacityLine);
  setPaint(pt, 'circle-opacity', opacityPt);

  // --- SIZE (point radius only — meaningless for polygons/lines) ---
  // Fallback is the zoom-interp expression so points stay legible at any
  // zoom. An explicit size channel overrides with a data-driven radius.
  const sizeExpr = channelExpr(spec?.size, feats, DEFAULT_POINT_RADIUS_EXPR);
  setPaint(pt, 'circle-radius', sizeExpr);

  // --- STROKE (polygon outline, line width, point stroke width) ---
  const strokeOutline = channelExpr(spec?.stroke, feats, 1.3);
  const strokeLine = channelExpr(spec?.stroke, feats, 1.8);
  const strokePt = channelExpr(spec?.stroke, feats, 1.0);
  setPaint(outline, 'line-width', strokeOutline);
  setPaint(line, 'line-width', strokeLine);
  setPaint(pt, 'circle-stroke-width', strokePt);
}

function setPaint(layerId, prop, value) {
  if (map.getLayer(layerId)) map.setPaintProperty(layerId, prop, value);
}

// Build a linear-interpolate expression for a channel spec. Returns the
// fallback value when the spec is missing or no numeric values are present
// in the column.
function channelExpr(chSpec, feats, fallback) {
  if (!chSpec || !chSpec.column || !Array.isArray(chSpec.range)) return fallback;
  const range = numericRange(feats, chSpec.column);
  if (!range) return fallback;
  const [lo, hi] = range;
  return ['interpolate', ['linear'], ['to-number', ['get', chSpec.column]],
          lo, chSpec.range[0], hi, chSpec.range[1]];
}

function numericRange(feats, col) {
  let lo = Infinity, hi = -Infinity, n = 0;
  for (const f of feats) {
    const v = f.properties?.[col];
    if (typeof v === 'number' && isFinite(v)) {
      if (v < lo) lo = v;
      if (v > hi) hi = v;
      n++;
    }
  }
  if (!n || lo === hi) return n ? [lo, lo + 1e-9] : null;
  return [lo, hi];
}

function distinctValues(feats, col) {
  const seen = new Set();
  const order = [];
  for (const f of feats) {
    const v = f.properties?.[col];
    if (v == null || v === '') continue;
    const k = String(v);
    if (seen.has(k)) continue;
    seen.add(k);
    order.push(v);
  }
  return order;
}


function renderLayerRow(name, color, meta, styleSpec) {
  const el = document.createElement('div');
  el.className = 'layer-row';
  const notes = meta.notes
    ? `<div class="meta-note"><em>${escapeHtml(meta.notes.slice(0,80))}${meta.notes.length>80?'…':''}</em></div>`
    : '';
  const legend = renderLegend(name, color, styleSpec);
  const authored = renderAuthored(meta);
  el.innerHTML = `
    <input type="checkbox" id="cb-${name}" checked data-layer="${name}"/>
    <label for="cb-${name}">
      <div class="layer-name">
        <span class="layer-swatch" style="background:${color}"></span>
        <span>${escapeHtml(name)}</span>
      </div>
      <div class="meta">${escapeHtml(meta.geometry_type ?? 'no geometry')} · ${meta.feature_count.toLocaleString()} features</div>
      ${notes}
      ${legend}
      ${authored}
    </label>`;
  const cb = el.querySelector('input');
  cb.addEventListener('change', (e) => toggleLayer(name, e.target.checked));
  layersEl.appendChild(el);
}

// Render a compact legend for the layer's active style: color swatches
// for categorical, gradient bar for linear, plus channel lines for any
// active size/opacity/stroke channel.
function renderLegend(name, color, styleSpec) {
  if (!styleSpec) return '';
  const parts = [];
  const info = dataLayers[name];
  const feats = info?.data?.features || [];

  // --- color legend ---
  if (styleSpec.column) {
    parts.push(`<div class="legend-col-label">${escapeHtml(styleSpec.column)}</div>`);
    if (styleSpec.scale === 'linear' && Array.isArray(styleSpec.palette)) {
      const range = numericRange(feats, styleSpec.column) || [0, 1];
      const [lo, hi] = range;
      const loC = styleSpec.palette[0];
      const hiC = styleSpec.palette[styleSpec.palette.length - 1];
      parts.push(
        `<div class="legend-gradient" style="background:linear-gradient(to right, ${loC}, ${hiC})"></div>
         <div class="legend-range">
           <span>${formatNum(lo)}</span><span>${formatNum(hi)}</span>
         </div>`);
    } else {
      const palette = styleSpec.palette || {};
      const entries = Object.entries(palette);
      if (entries.length) {
        parts.push('<ul class="legend-cats">');
        for (const [value, c] of entries.slice(0, 24)) {
          parts.push(`<li><span class="legend-swatch" style="background:${c}"></span>
                        <span class="legend-val">${escapeHtml(value)}</span></li>`);
        }
        if (entries.length > 24) {
          parts.push(`<li class="legend-more">+${entries.length - 24} more</li>`);
        }
        parts.push('</ul>');
      }
    }
  }

  // --- extra channels ---
  const chans = [
    ['size', 'size'], ['opacity', 'opacity'], ['stroke', 'stroke width'],
  ];
  for (const [key, label] of chans) {
    const ch = styleSpec[key];
    if (!ch || !ch.column || !Array.isArray(ch.range)) continue;
    parts.push(
      `<div class="legend-channel">
         <span class="legend-channel-key">${label}</span>
         <span class="legend-channel-val">${escapeHtml(ch.column)}
         <span class="legend-channel-range">${formatNum(ch.range[0])} → ${formatNum(ch.range[1])}</span></span>
       </div>`);
  }

  if (!parts.length) return '';
  return `<div class="legend">${parts.join('')}</div>`;
}

// Column-attribution line (LLM-authored vs derived). Shown when the
// server surfaces column_provenance in the layer summary.
function renderAuthored(meta) {
  const cp = meta.column_provenance;
  if (!cp || typeof cp !== 'object') return '';
  const byAuthor = {};
  for (const [col, info] of Object.entries(cp)) {
    const a = info?.authored_by || 'other';
    (byAuthor[a] = byAuthor[a] || []).push(col);
  }
  const lines = [];
  if (byAuthor.llm) {
    lines.push(`<span class="auth-dot auth-llm"></span>LLM: ${
      byAuthor.llm.map(escapeHtml).join(', ')}`);
  }
  if (byAuthor.derived) {
    lines.push(`<span class="auth-dot auth-derived"></span>derived: ${
      byAuthor.derived.map(escapeHtml).join(', ')}`);
  }
  if (!lines.length) return '';
  return `<div class="authored">${lines.join(' · ')}</div>`;
}

function formatNum(v) {
  if (typeof v !== 'number' || !isFinite(v)) return '—';
  if (Math.abs(v) >= 10000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (Math.abs(v) >= 10) return v.toLocaleString(undefined, { maximumFractionDigits: 1 });
  return v.toLocaleString(undefined, { maximumFractionDigits: 3 });
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
  // Slightly pad the hit box so point/line selection is forgiving even when
  // a polygon fill sits under them. queryRenderedFeatures returns everything
  // at these pixels; we rank below.
  const PAD = 4;
  const bbox = [
    [e.point.x - PAD, e.point.y - PAD],
    [e.point.x + PAD, e.point.y + PAD],
  ];
  const feats = map.queryRenderedFeatures(bbox, { layers: allIds });
  if (!feats.length) return;

  // Rank hits: prefer point > line > polygon-outline > polygon-fill. Within
  // a kind, prefer the smaller individual feature (so clicking where
  // Södermalm overlaps Gamla stan inside the same layer yields Gamla stan,
  // not whichever one MapLibre happened to draw on top). Final tiebreak by
  // layer bbox — catches edge cases where feature area is 0.
  function kindRank(id) {
    if (id.endsWith('-pt')) return 0;
    if (id.endsWith('-line')) return 1;
    if (id.endsWith('-outline')) return 2;
    if (id.endsWith('-fill')) return 3;
    return 4;
  }

  // Signed shoelace area of a single ring (lng/lat degrees — absolute value
  // is a monotonic proxy for geographic area at city scale). Good enough
  // for "smaller beats bigger"; not for cartographic accuracy.
  function ringArea(ring) {
    let a = 0;
    for (let i = 0, n = ring.length - 1; i < n; i++) {
      a += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1];
    }
    return Math.abs(a) / 2;
  }
  function featureArea(g) {
    if (!g) return 0;
    if (g.type === 'Polygon') {
      // outer ring minus holes
      let a = ringArea(g.coordinates[0] || []);
      for (let i = 1; i < g.coordinates.length; i++) a -= ringArea(g.coordinates[i]);
      return Math.max(0, a);
    }
    if (g.type === 'MultiPolygon') {
      let a = 0;
      for (const poly of g.coordinates) {
        a += ringArea(poly[0] || []);
        for (let i = 1; i < poly.length; i++) a -= ringArea(poly[i]);
      }
      return Math.max(0, a);
    }
    return 0;  // points / lines / etc. — kindRank already orders them above
  }

  // Keep per-feature entries (not per-layer) so same-layer overlapping
  // features can compete on their own area. Then dedupe by layer for the
  // "also at this point" listing.
  const candidates = feats.map((f) => {
    const srcId = f.source;
    const layerName = srcId.startsWith('src-') ? srcId.slice(4) : srcId;
    return {
      feat: f,
      layerName,
      kind: kindRank(f.layer.id),
      fArea: featureArea(f.geometry),
      lArea: dataLayers[layerName]?.areaEst ?? 0,
    };
  });
  candidates.sort((a, b) =>
    (a.kind - b.kind) || (a.fArea - b.fArea) || (a.lArea - b.lArea)
  );
  const top = candidates[0];
  const topName = top.layerName;
  const topFeat = top.feat;
  // Build "also at this point" from the remaining unique layer names.
  const other = [];
  const seenNames = new Set([topName]);
  for (const c of candidates.slice(1)) {
    if (!seenNames.has(c.layerName)) {
      seenNames.add(c.layerName);
      other.push(c.layerName);
    }
  }

  const color = dataLayers[topName]?.color || '#B05B3B';
  const props = topFeat.properties || {};
  const rows = Object.entries(props)
    .filter(([k, v]) => v !== null && v !== undefined && v !== '')
    .slice(0, 12)
    .map(([k, v]) => `<div class="row"><span class="k">${escapeHtml(k)}</span><span class="v">${escapeHtml(String(v))}</span></div>`)
    .join('');
  const otherHtml = other.length
    ? `<div class="meta">Also here: ${escapeHtml(other.join(', '))}</div>`
    : '';
  const html = `
    <div class="feature-popup">
      <div class="popup-title">
        <span class="layer-swatch" style="background:${color}"></span>
        <span>${escapeHtml(topName)}</span>
      </div>
      ${rows || '<div class="meta">No non-empty properties.</div>'}
      ${otherHtml}
    </div>`;
  if (currentPopup) currentPopup.remove();
  // Snap the popup to the feature's actual coordinate when a point wins.
  // queryRenderedFeatures uses a 4px hit box, so the cursor lngLat can be
  // a few metres away from the point — visually fine at city zoom, but
  // a sizable offset at street zoom. Polygon/line winners still anchor
  // at the click point (inside the geometry) because there's no single
  // "feature lngLat" that makes sense.
  let popupLngLat = e.lngLat;
  const g = topFeat.geometry;
  if (g && g.type === 'Point') {
    popupLngLat = g.coordinates;
  } else if (g && g.type === 'MultiPoint' && Array.isArray(g.coordinates)) {
    // Anchor to the nearest sub-point of the multi-point feature.
    let best = null, bestD = Infinity;
    for (const c of g.coordinates) {
      const dx = c[0] - e.lngLat.lng;
      const dy = c[1] - e.lngLat.lat;
      const d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = c; }
    }
    if (best) popupLngLat = best;
  }
  currentPopup = new maplibregl.Popup({ closeOnClick: true, maxWidth: '320px' })
    .setLngLat(popupLngLat)
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
