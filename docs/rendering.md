# Map rendering

Server-side PNG export via matplotlib + shapely. One tool, one output,
designed to end a session with an artefact the LLM can embed in a
document.

---

## Why render server-side at all

Claude can write prose, Word documents, and PDFs. Claude cannot:

- Fetch vector features from a session-scoped DuckDB.
- Reproject and render them in the correct CRS.
- Composite per-feature style channels (color/opacity/size/stroke) from
  an active spec.
- Add a scale bar at the right length for the map extent.

So `render_map` exists. Give it a list of visible layers; it returns a
PNG URL valid for 24 hours. Claude takes that URL and embeds the image
in a document it composes itself — no PDF-layout tool needed on our
side.

This follows the [AI-native principle](design): build the render (hard
for Claude alone), skip the document assembly (easy for Claude alone).

---

## What the output looks like

```
┌─────────────────────────────────────────────────────────────┐
│ GEODATA MCP · STOCKHOLM · SWEREF 99 18 00                   │
│ Stockholm stadsdelar, north vs south                        │
│                                                             │
│                                                             │
│              [paper background + vector layers]             │
│                                                             │
│                                                             │
│                                   sbk_admin_polygons · …    │
│                                   ▪ South                   │
│                                   ▪ North                   │
│                                                             │
│ [══════] 10 km                                              │
└─────────────────────────────────────────────────────────────┘
```

- Paper-toned (ivory #F5F0E8) background. No tiled basemap.
- Faint cartographer's-grid gridlines.
- Eyebrow label in terra.
- Serif title.
- Vector overlay using the session's active style spec (or auto palette).
- Right-column legend.
- Bottom-left scale bar in metres or kilometres, rounded to a clean
  figure.

Default dimensions: 1600 × 1000 px at 150 DPI. Typical output size
100-200 KB PNG.

---

## Why no tiled basemap

Two reasons:

1. **The systemd sandbox denies outbound egress.** Carto / Mapbox /
   MapTiler tile fetches would fail. Solvable by allow-listing
   CDN IP ranges, but that's brittle (CDN ranges churn) and opens a
   general-purpose egress channel.
2. **The paper palette is the visual identity.** A Carto Positron tile
   underneath pushes the overall colour toward grey; a paper ivory
   reads more like a field-journal excerpt. The editorial register is
   deliberate.

The viewer (not rendered; interactive) uses Positron because
orientation matters when you're panning and zooming. For a static
image, orientation comes from the scale bar + the bbox + the reader's
knowledge of the city.

---

## Style spec reuse

The same spec `show()` accepts is the one `render_map` reads:

```python
sess.visible_styles = {
    "stadsdelar": {
        "column": "era",
        "scale": "categorical",
        "palette": {"pre-1900": "#B05B3B", "post-war": "#6B7348"},
        "size": {"column": "population", "range": [4, 14]},
    },
}
```

Color channel: matplotlib translates `interpolate` for linear,
per-feature colour lookup for categorical. Auto-palette assignment
writes back into the spec (same mutation as the viewer).

Size channel: only meaningful for points (circle radius). Polygons
ignore it. Lines ignore it.

Opacity channel: multiplies the per-geometry default (polygon fill 0.5,
outline 1.0, line 1.0, point 1.0).

Stroke channel: scales `line-width` for polygon outlines and lines, and
`circle-stroke-width` for points.

All channels are linear interpolations over the column's range in the
loaded data. NaN / non-numeric / missing values fall back to the
default.

---

## How layers are drawn

```
For each layer, in caller order:
    Fetch features as {geom: shapely, props: dict}
    For each feature:
        resolve color, opacity, size, stroke via spec + fallback
        if Point: push into scatter batch
        if Line/MultiLine: LineCollection
        if Polygon: add a face patch + an outline patch + hole patches
    Emit batched scatter for all points in this layer
```

Polygons are drawn as two overlapping patches — one with a 0.5-multiplied
alpha for the fill, one stroke-only for the outline — so outlines stay
crisp even when the fill is translucent. Matches the viewer's treatment.

---

## Scale bar

Algorithm:

1. Compute the plot's horizontal data range `dx` in metres.
2. Target bar length ≈ 18 % of `dx`.
3. Round target down to the nearest 1/2/5/10 × 10^n — so the bar reads
   `100 m`, `500 m`, `5 km`, etc., not `847 m`.
4. Draw the bar bottom-left with end-caps and a text label above.

Produces a readable scale bar for any sane extent (city-block to full
Stockholm kommun).

---

## Legend

Column on the right, 18 % of figure width. For each layer:

- Header line with the layer name and the color column (terra, 9 pt
  sans-serif).
- Value swatches for categorical (10 pt body, truncated at 22 chars
  with an ellipsis).
- Gradient swatch for linear (single hi-colour square — a proper
  gradient bar is future work).
- One line per extra channel showing "size `<column>`, lo→hi".

Labels are truncated to 22 characters with an ellipsis to keep the
column from overflowing.

---

## Why matplotlib and not headless Chromium

Considered approaches:

- **Headless Chromium (Playwright / Puppeteer).** Loads the viewer URL,
  waits for tiles + layers, screenshots. Highest fidelity. Rejected
  because the hardened sandbox (filesystem isolation, syscall denylist,
  outbound-egress block) makes Chrome painful to run. Chrome also pulls
  in ~300 MB of binary dependencies that would otherwise not be needed.
- **MapLibre-native + Node renderer.** Similar fidelity. Adds a Node
  runtime to the Python-only stack. Also blocked by the egress policy
  for tile fetches.
- **Mapnik.** Heavy C++ stack, PostGIS-centric, poor DuckDB story,
  development velocity has slowed.
- **matplotlib + shapely.** Pure Python, sandbox-friendly, lightweight,
  enough for the editorial-PNG use case. Chosen.

Trade-off: matplotlib doesn't give us fancy label placement, curve
interpolation, or true tile rendering. For a one-page artefact over
vector data, the simpler stack is the right call.

---

## Integration with exports

`render_map` writes into the same `data/exports/<token>/<file>` path
that GPKG and parquet exports use. A fresh 12-char `secrets.token_urlsafe`
token is generated per render. The token directory is created lazily;
the PNG lands inside.

The usual 24-hour export TTL applies. After that the file is cleaned up
and the URL returns 410 Gone.

Path traversal is blocked at `serve_export` — tokens have to be
alphanumeric (+ `-`, `_`), filenames cannot contain `/` or `..`, and
the resolved path must stay inside `EXPORT_ROOT`.

---

## What's deferred

- **True gradient legend bar.** Currently we just show the high-stop
  colour as a single swatch. A 20-slice strip would be more readable.
- **North arrow.** The map is always north-up (EPSG:3011 is
  Cartesian-oriented) but adding an explicit N indicator is good form.
- **SVG output.** PNG is fine for most embeds. SVG would let the
  consumer zoom without pixelation. Not hard to add, not done.
- **Customisable backdrops.** A user who wants Positron tiles could get
  them if we allow-list Carto's CDN. Haven't done this.
- **Multi-frame export** (small-multiples for comparing facets). A
  natural follow-up but not in scope today.
