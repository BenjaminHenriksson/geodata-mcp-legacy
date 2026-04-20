# Geodata MCP — documentation

This is the reference and design record for the Stockholm Geodata MCP server
at `geo.benjaminhenriksson.com`. The server exposes Swedish open municipal
data (SCB DeSO, SBK Stadskarta, OSM addresses) to LLM clients through the
Model Context Protocol, with authoritative spatial computation, enforced
provenance, and a session-scoped viewer.

The site is an active artefact, not a frozen reference. What's written here
is what's true today, 2026-04-20. Older commits tell older stories; the
source of truth is the code.

---

## What to read, in what order

- **[Design philosophy](design)** — the *why*. AI-native design principles,
  why provenance is first-class, why sessions are reversible, why the
  viewer uses a paper-tone palette instead of dark-mode tiles. Start here
  if you're deciding whether this is a tool worth using (or stealing ideas
  from).

- **[Architecture](architecture)** — the *how*. Request flow, components,
  the sandbox posture, and the key trade-offs behind running DuckDB as a
  session-scoped spatial backend.

- **[Data model](data)** — the *what*. 66 datasets, the normalization
  pipeline, canonical join keys (desokod, regsokod, kommunkod), the
  2018→2025 DeSO bridge, and why everything is pre-projected to EPSG:3011.

- **[Tool reference](tools)** — every MCP tool with signature, semantics,
  and idempotency annotations. This is the API surface an LLM sees. Living
  in lockstep with the code.

- **[Sessions and persistence](sessions)** — how a session lives, dies,
  and gets rehydrated. Covers the on-disk DuckDB file, the JSON sidecar,
  checkpoints, and the soft/hard TTL model.

- **[Provenance](provenance)** — the two-level provenance model:
  layer-level (inherited from catalog sources) and column-level
  (tracking LLM-authored annotations vs derived expressions). Why this
  matters for trust.

- **[Viewer](viewer)** — the MapLibre-based viewer, the style spec with
  four visual channels, auto-refresh, click ranking, and how the
  legend is rendered.

- **[Map rendering](rendering)** — the server-side PNG renderer, the
  editorial-backdrop decision, and why it deliberately doesn't try to
  look like Google Maps.

- **[Roadmap and limitations](roadmap)** — what's missing compared to
  traditional GIS tools, what's solved, what's explicitly out of scope
  and why.

---

## Audience

These docs assume the reader is comfortable with:

- SQL (DuckDB specifically);
- Basic GIS vocabulary (CRS, geometry types, spatial joins);
- Python / MCP / HTTP fundamentals.

They do *not* assume familiarity with Swedish administrative geography.
Where terms like *DeSO*, *stadsdel*, or *kommunkod* appear, they're glossed
inline.

---

## Non-goals for this documentation

- **Not a SaaS prospectus.** There's no sales pitch; the design decisions
  are discussed honestly, including the places where things are rough.
- **Not a beginner GIS tutorial.** The focus is what's unusual about this
  MCP server, not general-purpose geodata concepts.
- **Not comprehensive ops.** Deployment, incident response, and security
  internals live in private docs in the repo. If you're reading this
  online, you're reading the public subset.
