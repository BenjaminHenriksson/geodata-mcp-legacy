# Design philosophy

The MCP exists to give an LLM the few capabilities it genuinely lacks when
working with Swedish municipal geodata. Everything else is deliberately
absent.

---

## The AI-native principle

*Expose only what the LLM cannot do on its own.*

Frontier LLMs (Claude, ChatGPT, Gemini, Qwen, and the rest; this
server is model-agnostic and any MCP-capable client works) can already:

- Generate prose, narrative, explanation.
- Write documents, slides, and PDFs with their own file-generation tools.
- Compose HTML, iterate on code, and produce shell scripts.
- Reason through a problem, break it into steps, and sequence calls.

What the model *cannot* do, by itself:

- Execute authoritative spatial SQL against a real CRS-aware engine.
- Join 66 locally-curated datasets without the transport being an
  inefficiency.
- Maintain a persistent, reversible workspace across turns.
- Render a styled map over fetched tile imagery.
- Attach and later verify provenance that survives export.

The MCP's surface is shaped by that second list. We don't provide a
"render a PDF report" tool because the LLM writes better reports than a
fixed template. We don't provide a "summarize this layer in prose" tool
because the LLM already has prose. We do provide `export(format='png')`,
because reprojecting to EPSG:3011 and compositing a styled vector overlay
on a paper-toned backdrop is a task the LLM can't do without us.

This principle rules out about half the tools one would naïvely implement
on a GIS backend, and it's why the tool surface is compact (11 tools)
despite covering a wide workflow. Sub-operations are multiplexed via
`op: Literal[...]` enums on tools like `derive`, `edit_field`, and
`layer` — the LLM's mental model is coarser than the underlying ops
(it thinks "I want a subset of this layer", not "should I call `filter`
or `top_n`"), and the surface matches that.

---

## Provenance is a first-class concern

Every layer that enters a session carries one or more `SourceRef` records
describing where its rows came from: dataset id, publisher, licence,
retrieved-on date. Derived layers (from `derive`, `execute_sql`) inherit
the union of their parents' source refs. A layer that comes out of
`load(op='inline', ...)`, geometry built from model-generated coordinates,
is flagged so `sources(layer)` can surface "this was made up by the
model" as a first-class fact.

On top of that, we track **per-column provenance**: when `edit_field`
writes a column (add / update / classify / annotate), the session
remembers who wrote it, with what expression, and optionally what
model. So an exported attribute can be traced to "LLM-written on
2026-04-19 by `edit_field(op='annotate', model='gpt-5')`" (or
`claude-opus-4-7`, or any other model identifier the calling client
passes), distinct from "loaded from `sbk_admin_polygons.gpkg`".

Why this matters:

- **Trust.** In a municipal context, a dataset that picks up uncited
  model-authored columns silently becomes compromised in a way that's
  hard to detect downstream. Surfacing the author per column makes the
  compromise visible.
- **Reproducibility.** The operation history + column provenance + source
  refs mean any layer's lineage can be reconstructed from the session
  state alone. No out-of-band notes needed.
- **Legal hygiene.** Export carries licence info per source, so a
  downstream consumer can check whether CC0 + ODbL + LM-data mixing is
  acceptable for their use.

The trade-off: every op carries provenance plumbing, so the code is
slightly heavier than a minimal "just run the SQL" implementation. This
has been worth it.

---

## Reversibility via scoped checkpoints

A session isn't a linear log of edits. It's a set of named savepoints
that can be rolled back independently.

    checkpoint(op="create", name="before_enrichment", layers=["buildings"])
    # ... edit_field(op="annotate" | "add" | "classify", ...) ...
    checkpoint(op="rollback", name="before_enrichment")   # undo just the buildings work

Checkpoints capture *column snapshots* (pre-images of the columns about
to be mutated) rather than whole-layer copies. Storage cost is
proportional to what's changed, not to the size of the layer. A
checkpoint on a 79k-building layer that only rewrites two columns stores
two 79k-row columns, not 158k rows of full-layer duplicates.

Two scoping modes:

- `checkpoint(op="create", name="x")` (no scope): covers every
  mutation on every layer until `op="commit"` or `op="rollback"`.
- `checkpoint(op="create", name="x", layers=["a", "b"])`: covers only
  mutations on those layers; other work is irreversible relative to
  this checkpoint.

Multiple checkpoints can be active concurrently, with overlapping or
disjoint scopes. A mutation snapshots once per covering checkpoint.
Rollbacks are independent.

Why this matters: LLM-driven enrichment is iterative and frequently
wrong. Without a scoped checkpoint model, a single bad
`edit_field(op="annotate", ...)` call becomes a session-ending event.
With it, the LLM can experiment cheaply.

---

## The session as workspace

Sessions persist across restarts. Each session has its own
`<id>.duckdb` file plus a `<id>.meta.json` sidecar holding Python-side
state (layer metadata, visible-style specs, checkpoint metadata, history,
cursors). On startup the registry rehydrates lazily: a client reconnects
with their session id, we reopen the DuckDB handle and read the
sidecar. Nothing is lost to a graceful service restart.

Idle sessions (30 minutes without a tool call) have their DuckDB handle
closed but keep their files. The hard TTL is 14 days. This is long enough
that "come back to an analysis tomorrow" is cheap, and short enough that
abandoned sessions don't pile up indefinitely.

The trade-off: persistent workspaces mean we can't use
`duckdb.connect(":memory:")`. Storage grows with active sessions. At
current scale (single-digit concurrent users, sub-1-GB per session) this
is a non-issue.

---

## The viewer is paper, not dashboard

The viewer uses warm editorial colours: ivory background, ink text, terra
accents, Cormorant Garamond for titles, DM Sans for body. Basemap is Carto
Positron (soft warm-grey) rather than Dark Matter.

This was chosen for two reasons:

1. **Continuity with benjaminhenriksson.com.** The portfolio site uses
   the same palette; the MCP's viewer shouldn't look like a different
   product.
2. **Shareability.** A paper-toned map reads as a field-journal excerpt.
   It sits comfortably next to text in a doc or a slide. Dark-mode
   dashboards read as surveillance tooling, wrong register for municipal
   planning work.

The layer colour palette is deliberately desaturated: cartographer's inks
(burnt umber, raw sienna, verdigris, antique brass) rather than the
neon-on-dark of dashboards. No colour is meant to "pop"; they coexist.

Rendered PNG exports (via `export(format='png')`) follow the same rules. No tiled
basemap (the systemd sandbox denies outbound egress anyway). Just paper
background, faint cartographer's grid, vector overlay, scale bar, and
legend.

---

## What the principle rules out

Applying the AI-native principle, here are things the server deliberately
*doesn't* provide:

- **Narrative summary tools** (`describe_findings`, `summarize_layer`).
  The model writes better prose than any template we could ship.
- **Report layout** (PDF composition, slide templating). Modern AI
  assistants have their own document-generation paths; we hand them a
  PNG and let them compose.
- **Natural-language query parsers** (`query_in_english`). Frontier
  models already understand English (and Swedish) and write DuckDB SQL
  directly.
- **Explanation-generation tools** (`explain_column`, `why_did_this_fail`).
  The LLM reasons over the data; we just provide authoritative data.
- **Conversational-UI helpers** ("friendly error messages"). Error
  responses are structured facts; presentation is the client's job.

This list matters because it kept the tool count from ballooning into the
hundreds that a naïve "expose every DB verb" approach would produce. A
smaller, sharper surface means fewer tools the LLM has to rank between,
fewer prompts for the router to wade through, and a clearer mental model
for the human debugging the flow.

---

## What the principle makes us build

And these are the capabilities the principle specifically motivates:

- **Spatial ops** (`derive`, `execute_sql`): authoritative CRS-aware
  geometry work over a DuckDB spatial backend.
- **Provenance tracking** (layer + column): trust infrastructure that
  survives export.
- **Viewer with auto-refresh.** A live rendering surface that the LLM
  can update by calling `layer(op="show")` again without the user
  touching anything.
- **Checkpoints.** A transactional workspace the LLM can experiment in.
- **PNG rendering** (`export(format='png')`): editorial map artefacts
  the LLM can embed.
- **Persistence.** Survives restart so paused analyses resume.
- **Canonical join keys** (added to every SCB table in the normalization
  pipeline): removes a class of join-column-name-guessing the LLM
  otherwise pays a tax on.
- **Bilingual schema** (Swedish + English per column description): the
  LLM works well in either but we can afford to give it both.

Every one of these is a thing the LLM couldn't accomplish with just text
generation. That's the filter.

---

## Trade-offs we accept

A few places the principle pulls against pragmatism:

- **No `render_pdf` tool.** The AI app's own file-tools can compose a
  PDF from a PNG + prose, but the round-trip is slower than a dedicated
  tool would be. We accept the cost.
- **Smaller tool surface means more LLM composition.** A user used
  to ArcGIS or QGIS might expect a "buffer + clip + dissolve" one-shot; we expose
  the primitives and let the LLM sequence them. This makes individual
  queries slightly slower but keeps the surface coherent.
- **Paper-tone map is not universally readable.** Users comfortable with
  OpenStreetMap's standard tiling might want the familiar view. We let
  them override via localStorage in the viewer, but the default leans
  editorial.
- **Provenance plumbing is everywhere.** Code is longer than a minimal
  implementation. The cost is felt every time a new operation is added.
  We've decided it's worth it.
