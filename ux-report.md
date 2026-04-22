# UX report: 38→11 tool refactor

Captured 2026-04-22 after the tool-surface refactor went live. Four
sub-agents ran independent, realistic tasks against the in-process
MCP, each with an isolated session, and then reported on friction.

## Tasks run

| Agent | Task | Tools exercised | Completion | Difficulty |
|---|---|---|---|---|
| A (income) | Top-5 Stockholm DeSOs by 2024 mean net income + population | `catalog`, `load`, `execute_sql`, `sources`, `inspect` | Yes | 2/5 |
| B (spatial) | Buildings within 300 m of Slussen → CSV with citations | `catalog`, `geocode`, `load`, `derive(filter)`, `inspect`, `export(cite=True)` | Yes | 2/5 |
| C (classify) | Buildings + era classification under a checkpoint, commit or rollback | `catalog`, `geocode`, `load`, `checkpoint`, `edit_field(add/classify)`, `execute_sql`, `inspect` | Yes (rollback) | 2/5 |
| D (render) | PNG map of top-10 DeSOs by population density with categorical style | `catalog`, `load`, `execute_sql`, `derive(top_n)`, `edit_field(classify)`, `layer(show+style)`, `export(format=png)` | Yes | 3/5 |

No agent gave up. No agent finished below "mostly smooth". The
refactor surface is fundamentally sound.

---

## Real bugs

### 1. `derive(op="top_n", by=…)` breaks on ordering keywords AND docs teach the broken pattern

Multiple agents hit this and the stress-test script initially did too.
The op wraps `by` in parens and unconditionally appends `DESC|ASC NULLS LAST`,
so:

```
derive(op="top_n", layer="deso_2025", by="ST_Area(geom) DESC")
  → ORDER BY (ST_Area(geom) DESC) DESC NULLS LAST
  → DuckDB ParserException
```

The `docs/tools.md` example literally shows
`"median_income DESC NULLS LAST"` as a valid `by` value — a doc trap
that poisons the first try. Pre-existing in `operations/query.py`,
not introduced by the 38→11 refactor.

**Fix direction**: detect and strip trailing `ASC|DESC (NULLS
FIRST|LAST)?` from `by`. If found, honor it as the direction. If
absent, fall back to `ascending` parameter. Update docstring
examples.

### 2. `sources()` returns `""` silently when the session has no layers

Agent A stumbled into this and lost time. The response shape is
`{"result": ""}` with no `warning`, `hint`, or diagnostic. For a
read-only citation tool, an empty response with zero signal is
actively unhelpful.

**Fix direction**: when `layer=None` and `session.layers` is empty,
return an explicit prompt: "No layers in this session. Call `load`
first."

### 3. Markdown-table key name is inconsistent across tools

Three different names for the same kind of field:

- `execute_sql` table mode → `markdown`
- `inspect(op="rows")` → `table_md`
- `docs/tools.md` execute_sql section → `markdown_table` (doc bug
  I introduced in the rewrite)

**Fix direction**: pick one (`table_md`), rename the other two,
update docs.

### 4. Doc drift on PNG rendering & basemap (resolved — doc-only)

Docs said "Paper-toned editorial backdrop (no tiled basemap — the
server sandbox denies outbound egress…)". Agent D's rendered PNG
clearly had OSM/Carto tiles with attribution, implying egress had
been relaxed. **Investigated on 2026-04-22; egress is NOT relaxed.**
Tiles are served from a pre-warmed local cache at
`data/basemap/positron/{z}/{x}/{y}.png` (~230 tiles, ~4 MB for
Stockholm kommun, zooms 10–13), populated offline by
`scripts/fetch_basemap.py`. The service unit's `IPAddressDeny=any`
is still load-bearing and enforced at runtime. The claim "no tiled
basemap" was never correct for renders with a populated cache; the
architecture was always "cached tiles, not live fetches".

**Fix applied**: `docs/rendering.md`, `docs/design.md`,
`docs/tools.md`, and the `hint` string in the `export(format="png")`
response updated to accurately describe the cache-based design and
the fallback path when the cache is missing.

---

## Design signals (API questions, not auto-fix)

### A. `export(format="png")` hides its coupling to `layer(op="show", style=…)`

Agent D's strongest feedback. The PNG renderer reads
`sess.visible_styles`, but the `export` signature gives no hint —
if you never called `show(style=…)`, you get a default render. The
folded `export` now has a heterogeneous arg list (`cite` /
`merge_geojson` are nonsense for PNG; `title` / `legend` /
`width_px` / `height_px` are nonsense for data exports).

**Options**: (a) re-split `render_map` out of `export` — reverses
part of the refactor, costs one slot; (b) add a PNG-only `style=`
override on `export` so data flow is self-contained; (c) keep as-is
and make the doc explicit. No auto-fix; decide deliberately.

### B. `edit_field` may fold too many shapes (resolved — write_attributes is its own tool)

Agent C: `add`/`update`/`drop`/`classify` all share `name` + `expr`
(or similar single-expression) signatures. The old `annotate` op had
a bulk-dict payload with nothing in common.

**Resolved 2026-04-22** (initial split), **renamed 2026-04-22** (round-2
follow-up): the bulk-write tool was promoted out of `edit_field` as
`annotate`, then renamed to `write_attributes` after a second sub-agent
flagged that "annotate" reads as "attach footnotes," not
"authoritative column writes." `edit_field` now carries only the four
expression-driven sub-ops (`add`/`update`/`drop`/`classify`), so its
parameter list is coherent (`name`/`expr`/`where`/`rules`/`default`).
`write_attributes` takes `values={key: {attr: val, ...}}`,
`key_column`, `dry_run`, `model`. Docs (`docs/tools.md`, README,
design, provenance, viewer, roadmap), the bulk-enrichment example in
`SERVER_INSTRUCTIONS`, and the stress test updated. Tool count:
12 → 13.

### C. `derive(op="select_by_location")` requires a `by_layer` — no bare point+radius

Agent B's natural first try was
`derive(op="select_by_location", center_3011=(x,y), distance_m=300)`.
Had to either materialise a 1-row point layer first, or fall back
to `derive(op="filter")` with `ST_DWithin(geom, ST_Point(…), N)`.
Extending `select_by_location` to accept a literal point would fold
one common pattern into one call.

### D. `load(op="catalog")` could accept a `point_3011 + radius_m` circular filter alongside `bbox_3011`

Agent B again — "fetch everything within N metres of here" is
common enough to warrant a first-class filter rather than bbox
gymnastics.

### E. `catalog(id=…)` could enumerate distinct values for small closed-set categorical attributes

Agent A spent a round-trip discovering that `inkomstkomponent` in
`scb_income_structure` has 17 specific values. The catalog already
enumerates `tabellinnehåll` values in descriptions — extending the
pattern would save the `SELECT DISTINCT` step.

---

## Positive signals (things that worked)

- **`op` enum pattern**: endorsed by all four agents. Agent C
  explicitly: "`checkpoint` as a single tool vs 3 dedicated tools —
  I'd keep the `op` enum." Agent D: "`op` dispatch reads well."
- **Reversibility hints on mutating responses**: Agent C — "genuinely
  useful." The `hint` string on `edit_field` responses spells out
  both commit and rollback incantations verbatim.
- **Canonical SCB join keys** (`desokod_2025`): Agent A found and
  used them without friction, saving a discovery detour.
- **`cite=True` on export**: Agent B — "delivered exactly what the
  task asked for in one call."
- **`edit_field(op="classify")` rules shape**: Agent C — "obvious
  and matches the example in `SERVER_INSTRUCTIONS`. Zero friction."
- **`load(op="inline")` mandatory `source`**: accepted without
  complaint across all tests that needed it.

---

## Minor signals (worth a note)

- Agent D: SCB 2018→2025 DeSO grid splits cause double-counts on
  naive joins. The existing footgun note says "use `desokod_2025`"
  but doesn't warn that multiple 2018 rows can collapse onto one
  2025 code — needs `SUM()` + `GROUP BY`. Add a concrete join
  example to `docs/tools.md`.
- Agent D: `layer(op="show", style=…)` categorical-palette legend
  ordering is palette-dict insertion order. Works, but should be
  documented.
- Agent B: `inspect(op="rows")` doesn't include `rowid` by default
  — only `op="batch"` does. Worth an opt-in toggle for users who
  want to sample + annotate without switching modes.
- Agent A: session state not hydrating across separate Python
  processes bit every agent (harness issue, not refactor bug).
  Real MCP clients are long-running so this doesn't surface there,
  but worth a note for anyone scripting against the server
  in-process.

---

## Status

- **Fixed in the same session as this report**: bugs 1, 2, 3, 4.
  (Bug 4 was a doc-only fix after security-posture investigation
  confirmed the sandbox was still enforcing egress-deny; the tiles
  come from a pre-warmed local cache, not runtime fetches.)
- **Design decisions open (originally)**: signals A, B, C, D, E.
  Status after round-2 follow-ups (see below): A, B, C resolved;
  D, E remain deferred as low-ROI.

---

## Round 2 — second sub-agent swarm (2026-04-22)

A second 4-agent swarm ran against the post-refactor 12-tool surface
(after `render_map` split + `annotate` split). Agents took 4 new
tasks (annotate workflow, point+radius spatial, DeSO 2018→2025
change detection, styled-map deliverable). All completed at 2/5
difficulty.

### Round-2 findings (resolved in the same session)

1. **Cross-process session ghost-table inconsistency** (3/4 agents).
   DuckDB's catalog persisted tables that `.meta.json` didn't know
   about, producing a confusing "Table X already exists" +
   "unknown layer X" pair. Fixed in `session.py`: `register()` now
   flushes the sidecar synchronously, and `get_or_create()` cleans
   up orphaned DuckDB + audit-log files when the sidecar is
   absent.

2. **`inspect(op="rows")` was markdown-only** (Agent Annotate,
   strong signal). Forced programmatic consumers to re-parse their
   own markdown. Response now carries `rows: [{col: val, ...}]`
   alongside `table_md`. Round-2 stress test pins this as a
   regression.

3. **`geocode(op="forward")` nondeterministic ranking** (Agent
   Radius). Same query produced different top matches for `limit=1`
   vs `limit=3`. Fixed by deterministic tiebreak
   (`ORDER BY score DESC, name ASC, GRUPP ASC`). Round-2 stress test
   pins this as a regression.

4. **`kommunkod` NULL invariant contradicted real data** (Agent
   DeSO Change). Catalog said NULL only when `region_kind != 'deso'`;
   retired 2018-code rows also had it NULL. Catalog descriptions
   (both `description_sv` and `description_en`) updated to describe
   the real behaviour and recommend filtering by `desokod_2025` /
   `region_kind` instead.

5. **Legend truncation in `render_map` PNG** (Agent Styled Map).
   Widened the legend column (right_margin 0.2 → 0.24) and raised
   `MAX_LABEL` 22 → 30.

6. **Rename `annotate` → `write_attributes`** (Agent Annotate).
   "Annotate" pulled the wrong way ("attach footnotes"); new name
   makes the "authoritative per-row column writes" semantics
   blunt. Same operation underneath.

### Round-2 signals deferred

- Default `include_rowid=True` on `inspect(op="rows")`: worth a
  future bump but not blocking.
- Qualify the SCB "aggregate before joining" warning with a list of
  pre-cleaned tables vs dirty ones: low effort, good doc polish.
- Rename/alias `derive(op="select_by_location")` with point+radius
  mode as `derive(op="within_radius", ...)`: current docs do call
  out the dual signature, agents found it; cosmetic.
- `render_map` inline `style=` shortcut: current two-step flow is
  idiomatic once discovered.
- `edit_field(op="classify")` echo band counts in response: saves a
  round-trip, trivial.

### Round-2 positive confirmations

- **`render_map` split from `export`**: Agent Styled Map — "the
  tool index line is explicit. I never hesitated. Splitting worked."
- **`write_attributes` (as split out)**: Agent Annotate — helped
  once the naming lands, which motivated round-2 rename.
- **Point+radius mode on `select_by_location`**: Agent Radius —
  "found it fast".
- **Canonical SCB join keys**: Agent DeSO Change — worked cleanly;
  new "aggregate before joining" footgun note was verified
  empirically and, interestingly, didn't bite for
  `scb_population_age_sex` (SCB zero-fills retired codes).

---

## Round 3 — third sub-agent swarm (2026-04-22)

Four fresh agents on four new task shapes against the 13-tool
surface after round-2 fixes landed. Tasks: mixed-provenance analysis
(user-injected rows joined to catalog data), derive+write_attributes
workflow on a district drill-down, deliberate error-path probing,
and open-ended "what's different about outer vs inner Stockholm?"
exploration.

All four completed at 2-3/5 difficulty. No gave-ups. Consistent
verdict across agents: the surface is production-ready; remaining
findings are small polish.

### Round-3 convergent findings

1. **`execute_sql` in table mode returns `rows: <int>` instead of
   the actual row data** (Agents Derive-Annotate + Explore,
   independently). Same shape mismatch the round-2 fix applied to
   `inspect(op="rows")`. Both agents fell back to parsing markdown
   when they needed to stitch multi-layer query results into a
   downstream call (most commonly `write_attributes`, which
   single-layer `inspect(op="rows")` couldn't satisfy). Fix: add a
   structured `rows: [{col: val, ...}]` list alongside `table_md`
   in `execute_sql`'s table-mode response.

2. **Silent "empty success" footguns** (Agents Errors + Explore).
   - `layer(op="show")` with ALL requested layers unknown returns
     `{unknown_layers: [...], visible_layers: []}` and **no
     `error` field** — a caller dispatching on `.error` sees
     success.
   - `execute_sql(sql=..., result_name="X")` when no geometry is
     detected silently drops `result_name` and returns table mode.
     No warning; caller thinks a layer was materialised.

   Fix: return `error: unknown_layer` (with `available`) when all
   requested `layer(op="show")` names are unknown; emit a `warning`
   on `execute_sql` when `result_name` was provided but the result
   was demoted to table mode.

### Round-3 individual findings

3. **`unknown_dataset` error is terser than docs promise** (Agent
   Errors). Missing `available` / fuzzy suggestions / `hint`. Docs
   describe this behaviour; code doesn't deliver. Should match
   `unknown_layer`'s pattern.

4. **`load(op="inline")` without `source` returns `op_failed`
   instead of `missing_arg`** (Agent Errors). Error-code
   miscategorisation that breaks agent-side branching logic.

5. **`semicolon` in `derive(op="filter", where=...)` produces an
   opaque parser error** (Agent Errors). sqlglot complains
   "Expecting )" because the server wraps the clause. Add an
   explicit up-front rejection matching `execute_sql`'s
   `_no_semicolon`.

6. **WKT axis-order is a silent footgun on `load(op="inline")`**
   (Agent Mixed). `POINT(x y)` with `EPSG:4326` means
   `POINT(lng lat)`; users think lat-first. A one-line docstring
   reminder would prevent silent axis-swap.

7. **`select_by_location` lacks a "mostly inside" predicate**
   (Agent Derive-Annotate). "Stadsdelar inside Södermalm district"
   is underserved: `within` (edge-touching excluded → 1 match) and
   `intersects` (shares-edge → 18 matches) both miss the intent.
   Added predicate `centroid_within`, or a `min_overlap_fraction`
   parameter, would cover the common case.

8. **`llm_sourced=True` mislabels user-injected-via-LLM data**
   (Agent Mixed). User brings field observations, LLM injects via
   `load(op="inline")`; provenance says the LLM authored it.
   Distinguish `"user"` vs `"llm"` as the author.

9. **Bulk reverse geocode** (Agent Mixed). `geocode(op="reverse",
   points=[...])` mirroring `inspect(op="at")` shape. Per-point is
   fine for 4, annoying for 200.

10. **`catalog(id=...)` attribute list isn't always exhaustive**
    (Agent Derive-Annotate). `sbk_admin_polygons` describe missed
    `GRUPP`, `KOMPONENT`, `RAMKLIPP`, `DNR`, `ADM_ID` — real
    columns on the loaded layer. Either make describe authoritative
    or flag "undocumented columns exist."

11. **Catalog fuzzy search misses Swedish demographic synonyms**
    (Agent Explore). English "immigration" doesn't surface
    `scb_*` tables named in Swedish (`utländsk bakgrund`).
    Indexing `invandring`, `utländsk` would close the gap.

12. **`sources()` structured-content key is `result`, not
    `markdown`** (Agent Explore). Minor naming mismatch with the
    tool's self-description as "provenance markdown."

13. **Checkpoint state is per-connection, not per-session-file**
    (Agent Derive-Annotate). Doesn't bite real MCP use (long
    connection); does bite scripted / split-process harnesses.
    Document or fix.

### Round-3 positive confirmations (prior interventions held)

- **Session sync** (round-2 fix): no agent hit the ghost-table
  pair. Fix stayed landed.
- **`inspect(op="rows")` structured `rows` list**: Agent Mixed used
  it naturally for the sample → write_attributes pipeline. "Joining
  injected data to catalog data was smooth once I had the
  geometry reprojected."
- **`write_attributes` rename**: Agent Derive-Annotate — "the
  session/checkpoint model + `write_attributes` coupling is
  genuinely nice." Agent Mixed used it without friction.
- **`geocode(op="forward")` stability**: Agent Errors probed
  recovery paths and didn't flag ranking issues. Fix held.
- **`render_map` split from `export`**: no rendering friction
  this round.
- **Canonical SCB join keys + aggregate-first note**: Agent Mixed —
  *"that warning is the single most valuable piece of the
  instructions."* Agent Explore followed the recipe cleanly and
  city-total reconciled to the person.
- **Rich-default layer summaries** (`quick_stats` + `sample` +
  `provenance`): Agent Explore — "didn't need to follow up with a
  separate `inspect`."

### Status after round 3

- **Fixed earlier this session**: round-1 bugs 1–4 (top_n,
  sources-empty, markdown-key, basemap-docs); round-1 signals A
  (render_map split) and B (write_attributes split, later renamed);
  top-tier conveniences (point+radius, include_rowid, SCB split
  example, legend ordering, session scripting caveat); round-2
  fixes 1–6 (session sync, structured inspect rows, geocode
  stability, kommunkod docs, legend width, write_attributes
  rename).
- **Round-3 findings**: not auto-fixed. The surface is production-
  ready as-shipped; round-3 priorities are listed above for a
  future pass if desired. Convergent findings (1) and (2) are the
  strongest candidates for another batch.
- **Long-tail deferred** from round 1: signal D (load point+radius
  filter — largely redundant given `select_by_location` point+radius
  mode), signal E (`catalog(id=…)` distinct_values enumeration —
  higher-effort catalog-schema bump).

Test harness: `scripts/stress_test_tools.py` (57 covering cases,
all passing post-fix).
