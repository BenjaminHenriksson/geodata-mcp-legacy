# Viewer

A MapLibre-based live-refreshing map surface at `/view/<session_id>`.
The viewer is a thin client: it polls the server for session state and
renders whatever is there, with no logic for knowing *what* the LLM is
doing, only what layers are currently visible.

---

## Structure

Two files, plus the MapLibre GL CDN:

- `viewer/index.html` — container markup and inline CSS, in the paper
  palette.
- `viewer/app.js` — all runtime logic.

The server serves the HTML at `/view/<sid>` with the JS path
cache-busted by a content hash (`/static/app.js?v=<hash>`). Cloudflare
will happily cache the JS file for a day; the hash query string
invalidates the cache when we ship a change.

---

## Basemap

Default: Carto **Positron** (warm-grey vector-ish raster tiles). Paper
palette reads well against it.

Override: a MapTiler or Mapbox style URL dropped into the basemap input
is stored in `localStorage.geodata_basemap_style_url` and used on
subsequent loads. Reset clears the key.

No default API key is baked in. The fallback is Carto's
attribution-required public CDN.

---

## The render loop

```
On page load
  ├── construct map (center 59.33°N 18.07°E, zoom 11)
  ├── fetch /api/<sid>/visible_layers → set title, styles, render layers
  ├── fit bounds to the tightest visible layer
  └── attach click handler

Every 2 seconds
  └── fetch /api/<sid>/version
        └── if version changed: re-run syncSession()
              └── diff visible_layers set:
                    ├── removed layers → removeLayer()
                    ├── same-layer-different-data → setData()
                    └── new layer → addLayer() + reorderLayers()
```

The 2-second poll is cheap (tiny endpoint, no DB touch unless rehydrate
is needed), and it keeps the viewer responsive to LLM tool calls
without needing a persistent WebSocket.

---

## Layer drawing

Each GeoJSON `FeatureCollection` becomes four MapLibre sublayers:

- `<name>-fill` — polygon fill.
- `<name>-outline` — polygon outline (separate sublayer for independent
  styling).
- `<name>-line` — LineString / MultiLineString.
- `<name>-pt` — Point / MultiPoint.

Features are filtered into the appropriate sublayer by geometry type.
This overhead is cheap and makes per-geometry-type styling trivial.

---

## Style spec

`show()` accepts `style={"<layer>": {...}}` and the viewer consumes it
directly. Four visual channels:

### Color channel

```
column: "<attribute>"           // column to color by
scale:  "categorical" | "linear"
palette:
    // categorical — lookup table, or omit to auto-assign from the
    // cartographer-ink palette
    {"value1": "#rrggbb", "value2": "#rrggbb"}
    // linear — two-stop gradient
    ["#lo", "#hi"]
```

Categorical auto-assignment mutates the `spec.palette` in place after the
first render, so the legend reads the same assignment the map uses.

### Size, opacity, stroke channels

```
size:    {column: "<attr>", range: [lo, hi]}   // circle-radius
opacity: {column: "<attr>", range: [lo, hi]}   // fill/line/circle opacity
stroke:  {column: "<attr>", range: [lo, hi]}   // line-width, circle-stroke-width
```

Each is a linear-interpolate expression over the column's numeric range in
the loaded data. Non-numeric or missing values fall back to a sensible
default.

Double-encoding is common: color by category, size by population, gets
twice the information density at no ink cost. Three channels at once
(the maximum) is usually too busy.

### Why this schema instead of Mapbox-style raw expressions

An LLM can author the Mapbox expression language directly, but that
leaks a lot of MapLibre implementation into the tool surface. Our
schema is:

- Declarative (says what the mapping is, not how to compute it).
- Small enough to fit in a prompt.
- Round-trippable server-side — the PNG renderer reads the same spec.
- Extensible per-channel without changing the top-level contract.

Trade-off: we can't express everything MapLibre can. Step functions,
match-with-defaults beyond what we wire, and other advanced patterns
need to be added to our schema if we want them.

---

## Legend

Auto-rendered from the active style. For each layer:

- One header line with the layer name and the color-column name (if the
  layer is styled).
- For categorical: a 2-column grid of value-swatch pairs, truncated at
  24 entries with a "+N more" footer.
- For linear: a small gradient rectangle with lo/hi numeric labels.
- Each extra channel (size/opacity/stroke) gets a one-line "size:
  `<column>`, range lo→hi" note.

The palette the legend shows is exactly what the map shows. Categorical
auto-assignment writes back to `spec.palette` so the two agree.

---

## Click ranking

Clicks hit everything under the cursor — often multiple features across
multiple layers. The ranker:

1. `queryRenderedFeatures` with a 4-px-padded hit box (so point and line
   selection stays forgiving).
2. Rank candidates by geometry kind: point < line < polygon-outline <
   polygon-fill.
3. Within a tie, prefer the smaller-area feature (shoelace on the rings).
4. Within that, prefer the smaller-layer-bbox layer (stable for edge
   cases with zero-area features).

The winner gets the popup. Other layers at the same point are listed in
an "also here" line below. This is the single most noticeable UX polish
in the viewer; before it, clicking Gamla Stan when it overlaps
Södermalm (both in `sbk_admin_polygons`) would often return whichever
MapLibre drew on top.

---

## Popup format

- Layer name with a color swatch in the palette colour, Cormorant 18px
  header.
- Up to 12 non-null, non-empty attributes as terra-key → ink-value rows.
- Key labels uppercase tracked 10px DM Sans. Values tabular-nums.
- "Also here" footer in ink-faint.

No raw geometry. No feature id. The LLM can fetch those via `inspect` if
needed.

---

## Column attribution line

Below the meta row, a small "authored" line:

- `LLM: era, note` — columns written by `annotate`.
- `derived: slope_deg` — columns written by `add_field` or
  `update_field` with a SQL expression.

These only appear if the server's `_layer_summary` includes
`column_provenance`. It's a visual cue that the layer carries model- or
expression-authored data, not purely loaded data.

---

## Audit panel

Top-right (`#panel-audit`, collapsible). Renders the per-session
operation timeline so the user can verify what the LLM actually did
without reading code.

For each operation (newest first):

- Tool name → result layer with status + duration (e.g. `filter →
  sthlm_deso  ok · 14:32:11 · 46 ms`).
- The LLM-supplied `description` in italic. Operations called without a
  description show a terra-edged "no description provided" line — a
  visible nudge that the LLM should be filling these in.
- A collapsible "N sql statements" block listing the captured SQL with
  per-statement duration. Internal probes (`DESCRIBE`, bbox aggregates,
  `COUNT`) are hidden by default; the "Show internal SQL probes"
  checkbox surfaces them.

Data source: `GET /api/<sid>/audit_log[?include_internal=1]`. Refreshed
on the same 2 s version-poll the rest of the viewer uses — no
additional polling cadence.

The full record (every SQL statement, with timing and status) is also
mirrored to disk at `<sid>.audit.jsonl` in append-only form, so the
panel rehydrates correctly after a server restart.

---

## Responsive behaviour

- **Desktop:** Layer panel fixed top-left at 320 px. Basemap settings
  bottom-left. Audit panel top-right at 360 px (collapsible). Map fills
  the rest.
- **Below 720 px:** Layer panel becomes a bottom-anchored sheet spanning
  full width, `max-height: 45vh`, with scrolling. Basemap settings
  migrate to the top-right and collapse by default. The audit panel is
  hidden — viewing audit history on phone-narrow widths isn't useful;
  open the viewer on a desktop for that.

There's no "hide panel" toggle for the layers and basemap. The map
always has panel overlay. If someone wants a clean map for a
screenshot, `render_map()` is the answer.

---

## Auto-refresh cadence

- `/api/<sid>/version` every 2 seconds while the tab is visible.
- On version diff, fetch `/api/<sid>/visible_layers` (session metadata
  + per-layer summaries + style specs) AND `/api/<sid>/audit_log`
  (operation timeline for the audit panel).
- For each new-or-changed layer, fetch the streaming GeoJSON endpoint
  `/api/<sid>/layer/<name>/geojson`.

The GeoJSON endpoint streams features in 2000-feature chunks from the
server (DuckDB `json_object` per feature, assembled on the Python side,
sent as chunked HTTP). This avoids the OOM-the-session-DB problem we hit
the first time we tried `json_group_array` on 79 k buildings.

The Page Visibility API pauses polling when the tab is hidden, so the
viewer doesn't burn CPU when it's not being looked at.

---

## MapLibre-specific trade-offs

- **MapLibre vs Leaflet.** MapLibre is heavier but supports vector
  tiles, GPU styling, and paint expressions that make the style spec
  implementation clean.
- **Server-side GeoJSON vs vector tiles.** We send raw GeoJSON because
  the layers are small (usually < 100 k features), vector tile
  generation is a build step we don't want, and MapLibre handles
  GeoJSON sources transparently.
- **No clustering.** If a layer has > 10 k points, the map gets visually
  dense. We haven't added clustering because the LLM is better at
  filtering the layer upstream via `filter()` or `top_n()` than the
  viewer is at collapsing points post-hoc.
