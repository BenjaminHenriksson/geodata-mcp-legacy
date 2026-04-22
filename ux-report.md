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

### B. `edit_field` may fold too many shapes (resolved — annotate split out)

Agent C: `add`/`update`/`drop`/`classify` all share `name` + `expr`
(or similar single-expression) signatures. `annotate` has a
bulk-dict payload with nothing in common.

**Resolved 2026-04-22**: `annotate` promoted to its own top-level
tool. `edit_field` now carries only the four expression-driven
sub-ops (`add`/`update`/`drop`/`classify`), so its parameter list
is coherent (`name`/`expr`/`where`/`rules`/`default`). `annotate`'s
data-driven shape (`values={key: {attr: val, ...}}`,
`key_column`, `dry_run`, `model`) lives on its own tool where those
params aren't diluted. Docs (`docs/tools.md`, README, design,
provenance, viewer, roadmap) and the bulk-enrichment example in
`SERVER_INSTRUCTIONS` updated accordingly. Tool count: 12 → 13.

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
- **Design decisions open**: signals A, B, C, D, E — to be decided
  deliberately, not auto-applied.

Test harness: `scripts/stress_test_tools.py` (57 covering cases,
all passing post-fix).
