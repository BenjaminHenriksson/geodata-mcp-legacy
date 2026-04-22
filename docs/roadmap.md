# Roadmap and limitations

Where the server is strong, where it's weak, and what's explicitly out
of scope. Honesty over ambition.

---

## Where the server is genuinely strong

- **Tabular + point/polygon reasoning** over Stockholm demographic and
  city-plan data. SCB joins with canonical keys, SBK polygons,
  building-level analysis. The 80-percent case of municipal work.
- **LLM-written attribute columns** with tracked provenance. The
  `edit_field(op="annotate") → checkpoint(op="commit") → export →
  sources` loop is clean and trustworthy.
- **Reversible experimentation** via scoped checkpoints. The LLM can
  try things and back out cheaply.
- **Session persistence** across service restarts. A paused analysis
  resumes where it left off, even after a graceful service restart.
- **Editorial PNG export** that embeds cleanly in reports, slides, or
  messages. Produces a publishable artefact, not a debug screenshot.
- **Provenance that survives derivation and export.** Every layer knows
  its lineage; every in-session column knows its author.

---

## What's missing compared to traditional GIS

Honest inventory of feature gaps, ranked by how often they block a
practical question.

### Tier 1: blocks a whole category of questions

- **Raster / elevation.** No DEM, no orthophoto, no Lidar. Slope,
  viewshed, rooftop-solar, urban-heat, sightline analysis, all
  unreachable. DuckDB spatial has limited raster support; nothing is
  loaded. One DEM + two tools (`elevation_at`, `slope_aspect`) would
  unblock the slope/terrain class of questions. Not hard to add;
  deferred.
- **Routing / isochrones.** OSM addresses are loaded as points, not as
  a network graph. No "15 minutes walking" or "catchment around this
  station" questions. Adding a routing engine (Valhalla or
  osmium+custom) is its own multi-day plan.
- **Detaljplan / ÖP layers** (zoning, current and proposed). The
  Stockholm dataportal has some ÖP layers with year-varying schema; we
  haven't ingested them. This is the core SBK workflow data gap, and
  it needs a bespoke normalization pipeline to handle schema drift.

### Tier 2: blocks common but specific needs

- **Geometry editing / digitization.** Can't draw a proposed building
  footprint, split a feature, snap to an edge, edit a vertex. Needs a
  viewer-side editing mode and round-trip API. Bigger frontend project,
  not a single tool.
- **Labeling on the viewer.** Features render; names don't appear on
  the map. Cartographic label placement is its own problem domain
  (overlap avoidance, priority weighting, placement zones). Not done.
- **Spatial statistics.** No Moran's I, Getis-Ord, Ripley's K. The
  "is this pattern clustered, and is it significant" class of
  questions. Most can be built with `execute_sql` + numeric work in
  the model's code-interpreter sandbox (Claude's, ChatGPT's, Gemini's;
  they all have one now), but having first-class tools would shorten
  the loop.
- **Fastighetsregister (property ownership).** Ownership + lot numbers
  + legal descriptions. Data is Lantmäteriet, licensed, not open. Would
  need a separate licensing arrangement.

### Tier 3: nice to have

- **WMS/WFS consumption.** Pull in a Lantmäteriet WMS layer without
  re-ingesting. Current egress policy blocks this; would need either
  IP allow-listing or a proxy.
- **Multi-user collaboration within one session.** Two users
  working the same workspace simultaneously. Current sessions are
  single-owner by design.
- **3D / elevation extrusion.** Would require the raster tier first.
- **Audit log for `execute_sql`.** Every SQL escape-hatch call recorded
  to a separate log, independent of the session history. Noted in docs
  as pending.

### Tier 4: explicit non-goals

- **SaaS multi-tenancy.** The system is designed for a small set of
  trusted users with a shared invite code. Making it tenant-safe is a
  different project.
- **Write-back to source systems.** We don't push changes back into
  SCB, SBK, or OSM. Our layers are derived, and they stay derived.
- **Real-time streaming.** No Kafka, no websocket push for new data.
  Everything is request-response.

---

## Product-shape gaps (not feature gaps)

- **No rate limiting beyond per-IP.** A compromised OAuth-authenticated
  client can issue many calls before hitting the bucket.
- **No per-user invite codes.** Everyone shares one invite code;
  there's no way to revoke a single user.
- **No session-sharing affordance in the viewer.** If you want
  someone else to see the map, you copy the URL. There's no "invite
  to view" button.
- **No docs-version mechanism.** This documentation describes what's
  true today. Historical versions of the system have to be
  reconstructed from git.

---

## What a "next tranche" actually looks like

Roughly in priority order, by impact-per-day-of-work:

1. **Detaljplan / ÖP ingestion.** Biggest data gap for the actual SBK
   workflow. Needs a careful normalization pipeline with schema
   detection across years.
2. **Elevation + 2 terrain tools.** Unblocks the slope class. Data is
   open from Lantmäteriet. One fetch script, one loader tweak for
   raster, two operation tools (`elevation_at`, `slope_aspect`). ~2 days.
3. **Routing + isochrones.** Biggest accessibility-analysis unlock.
   Heavy; requires a routing engine. ~4-5 days.
4. **Audit log for execute_sql.** Plumbing task; straightforward. ~½ day.
5. **SVG output from `render_map`.** Small; adds a format alongside PNG. ~½ day.
6. **Viewer labelling.** Non-trivial; label placement is its own
   topic. Defer until the demo actually needs it.

---

## Trade-offs worth revisiting

Some of the current decisions could be reversed if the use case
demands:

- **Outbound-egress deny.** The sandbox currently blocks the PNG
  renderer from using tile basemaps, blocks WMS consumption, blocks
  Nominatim fallback. If confidence in the `_validate_sql` SQL
  sandbox and the overall hardening improves, a targeted egress
  allow-list is defensible.
- **Paper-tone default map.** Some users will want standard OSM
  tiles for orientation. The viewer lets them override; the renderer
  doesn't. Could add a `style="positron"` option to
  `render_map` post-egress-relaxation.
- **Single invite code.** Serviceable for small demos; breaks for 50+
  users. Per-user codes with expirations is the natural next step.
- **No session ACL.** Anyone with a viewer URL sees its contents. This
  is fine for open data; becomes a problem the moment we ingest
  anything sensitive.

---

## What the docs don't cover

- **Operational runbook.** Token rotation, backup, service restart,
  how to diagnose an OOM. Lives in private docs in the repo
  (`_deployment.md`).
- **Security findings and mitigations.** The post-OAuth audit results,
  the systemd hardening details, credstore rotation. Lives in private
  docs (`_security.md`).
- **Implementation details below the public API.** The
  `_validate_sql` parser internals, the OAuth authorization code
  handling, the viewer's click ranker shoelace algorithm. Read the
  code.

If you find a gap in the public docs that you think should be covered,
mention it. This document and its siblings are living; nothing here is
fixed.
