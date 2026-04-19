# Architecture

How the pieces fit together and why each piece is where it is.

---

## Request flow

```
MCP client (Claude)         Browser (viewer)
      │                            │
      │ HTTPS                      │ HTTPS
      ▼                            ▼
              CDN / edge (TLS termination)
                         │
                         ▼
          Reverse proxy (TLS, routes, egress to app)
                         │
                         ├── /mcp, /mcp/*
                         ├── /oauth/*
                         ├── /.well-known/*
                         ├── /view/<sid>
                         ├── /api/<sid>/*    all forward to the
                         ├── /exports/<tok>  app on loopback
                         ├── /static/*
                         ├── /docs, /docs/<slug>
                         ▼
                geodata-mcp (uvicorn + FastMCP + Starlette)
                         │
                         ├── OAuth middleware (bearer or OAuth 2.1 token)
                         ├── Rate limit middleware (per-IP token bucket)
                         ├── REGISTRY → per-session DuckDB + sidecar on disk
                         └── static viewer assets
```

The Python app binds only to loopback. Everything public goes through
the reverse proxy, which terminates TLS. Specific proxy / CDN choices
are a deployment concern, not part of the public contract.

---

## Components

### `catalog.py` — the dataset registry

Loads `catalog.json` at startup, validates entries against the
`DatasetEntry` dataclass, builds a fuzzy-search corpus (Swedish + English
combined). One entry per dataset; the full list is what `search_data`
and `describe_dataset` work over.

Key fields per entry: `id`, `name_sv`/`name_en`, descriptions,
`source_type` (gpkg / parquet), `file_path`, `geometry_type`,
`feature_count`, `coverage`, `temporal`, attributes with sample values,
licence metadata, and an optional `dedupe_hint` for datasets with
non-obvious row-level quirks.

### `loader.py` — getting data into a session

Reads a normalized parquet/geopackage file into DuckDB as a layer.
Applies optional `bbox_3011`, `where`, and `intersect_layer` filters
before the per-load cap (100 k features). Records provenance from the
catalog entry. Everything is pre-projected to EPSG:3011; there's no
CRS guessing at load time.

### `operations.py` — the workhorse

All the verbs the MCP exposes — filter, spatial, stats, execute_sql,
macro helpers, annotate, add_field, checkpoint/rollback/commit, exports,
frequencies. Every function is session-scoped; there is no global DuckDB
connection.

Structure: thin SQL generators + parse-aware validation. `sqlglot` parses
user-supplied predicates to reject multi-statement input, function calls
to filesystem readers (`read_csv`, `read_text`, etc.), and a specific
denylist of DuckDB intrinsics we don't want LLM-invoked. This is the
server's primary defence against SQL-injection dressed as "friendly
expression" input.

### `session.py` — per-client state

Each MCP client connection gets a `Session` with its own DuckDB file,
layer metadata, visible styles, operation history, cursors, and
checkpoints. The `SessionRegistry` manages lifecycle: create, rehydrate
from disk, idle close, hard TTL delete, flush on shutdown.

Persistence is two-file per session:

- `<id>.duckdb` — the DuckDB file with tables (layers + checkpoint
  snapshots);
- `<id>.meta.json` — Python-side metadata that DuckDB doesn't know about
  (LayerMeta, visible_styles, history, etc.).

See [Sessions](sessions) for the lifecycle in detail.

### `server.py` — the MCP surface

Each tool is a thin `@mcp.tool`-decorated function that:

1. Resolves the session from the MCP context.
2. Calls into `operations.py` or `loader.py`.
3. Attaches a checkpoint hint if the target layer is under one.
4. Handles `SessionExpired` and other structured errors.

Tools are annotated (`_READ_ONLY`, `_SAFE_MUTATION`,
`_IDEMPOTENT_MUTATION`) so MCP clients can auto-approve the safe ones
without per-call permission prompts.

The server also mounts the viewer API (`/api/<sid>/...`), the static
viewer (`/view/<sid>`), the landing page (`/`), the docs
(`/docs`, `/docs/<slug>`), and the OAuth flow.

### `oauth.py` — invite-code-gated OAuth 2.1 + PKCE

Implements RFC 7591 dynamic client registration, RFC 8414 authorization
server metadata, RFC 9728 protected resource metadata, `authorize` and
`token` endpoints. Gate: a shared invite code entered on the consent
form. Legacy Claude Code clients can skip OAuth and present a
pre-provisioned bearer instead.

Secrets (invite code, legacy bearer) are delivered through a
credential manager at process start, so they never appear in the
process environment.

### `geocoder.py` — place-name lookup

Two-mode geocoder: composite address matching (street name + number with a
250 m spatial pairing) and place-name matching against `NamnText_point`
with Jaro-Winkler similarity. Returns bbox + centroid in EPSG:3011.

### `render.py` — server-side PNG rendering

Matplotlib + shapely. Reads features via the session's DuckDB connection,
draws on a paper-toned axes with scale bar, legend, and title. No tiled
basemap because the sandbox denies egress and the editorial palette
reads better without one.

### `viewer/` — static frontend

Two HTML files plus `app.js`. `landing.html` is the `/` page. `index.html`
is the per-session viewer served at `/view/<sid>`. The viewer polls
`/api/<sid>/version` every 2 seconds and re-fetches layers on diff, so
an LLM's `show()` calls update the user's open tab without interaction.

---

## Trade-offs

### DuckDB as the spatial engine

**Why DuckDB.** Pure Python embedding, in-process spatial via the
`spatial` extension, sub-second ingestion of 100k-feature parquet,
proper typed columns, SQL dialect close enough to PostGIS that people
can use it without training, excellent OGR interop for reading GeoPackage
and writing exports.

**What we give up.** No raster support (or very limited). No network
analysis (no pgRouting equivalent). No concurrent multi-user access to
the same DB (each session is single-owner). Spatial SQL functions are a
subset of PostGIS — some advanced operations aren't there yet.

**Why it works anyway.** The workload is read-heavy, session-scoped,
and sized well under DuckDB's sweet spot (hundreds of MB in a session,
not GB). Spatial extension covers the 80% of GIS ops that cover 99% of
municipal questions.

### FastMCP over writing the protocol ourselves

**Why FastMCP.** Streaming HTTP, decorator-based tool registration,
batteries-included lifespan + session management (which we wrap but
build on). Time-to-working-server was short.

**What we gave up.** Some coupling to FastMCP's internal conventions —
tool annotations, context injection, error shapes. Version bumps
occasionally force adjustments.

**Net assessment.** The library did more good than harm. A from-scratch
streaming-HTTP MCP implementation would have doubled the codebase and
delayed everything.

### Session-per-client instead of shared DB

**Why per-session.** Isolation (one client's noisy SQL can't OOM
another's), per-session memory + thread caps via DuckDB settings,
straightforward rollback (a checkpoint rolls back only that session's
state), and cheap session deletion (just unlink two files).

**What we gave up.** Multi-user collaboration within one session. If two
people want to look at the same analysis, they share the viewer URL but
can't both tool-call against it. For the demo's intended use case this
is fine.

### Hardened sandbox with egress denied

**Why deny all outbound.** The MCP runs untrusted LLM-generated SQL.
Even with `_validate_sql`, a motivated attacker who found a bypass
would otherwise gain full egress. Locking the process to localhost
means the worst outcome is local data manipulation, not exfiltration or
C2.

**What we gave up.** The render pipeline can't fetch tile imagery. Any
future tool that needs an external API call (routing, geocoding against
Nominatim, translation) has to either ship its data locally or relax
the policy. The current decision is to ship data locally.

**Alternative considered.** An IP allow-list with a tight set of CDN
ranges for Carto / OSM tiles. Rejected as brittle — CDN ranges change,
and an allow-listed host can still be an exfiltration channel if the
attacker controls what the process POSTs.

### OAuth 2.1 + invite code

**Why invite code.** The user base is small and trusted. An anonymous
world-readable endpoint would attract scrapers, LLM spamming, and
liability for data-hoarding claims against Swedish open-data licences.
An invite gate is the cheapest reliable filter.

**Why OAuth around it.** claude.ai's custom-connector flow wants OAuth
2.1 + PKCE. Making the invite code a pre-flight gate in the consent
form means we get both: the LLM-industry-standard auth ceremony and
the invite-level access control.

**What we gave up.** No per-user tokens (everyone with the invite
bootstraps into the same authorization). No granular scopes. For the
current use case this is acceptable.

### Viewer as session-private URL, no auth

**Why unauthenticated.** The URL is a 96-bit UUID; guessing is
computationally infeasible. Sharing the URL is how you collaborate.
Gating with a password would break the click-to-share property.

**What we gave up.** If someone pastes the URL in Slack and a bystander
sees it, that bystander has read-only viewer access for as long as the
session lives. That's fine given the open-data nature of everything in
it. If we ever host sensitive data, this decision revisits.

---

## Repository layout

```
geodata_mcp/                    Python package (server, operations, session, render)
viewer/                         Static assets — landing, viewer, docs shell
catalog.json                    Dataset registry (read-only at runtime)
data/
├── raw/                        Original downloaded files (gitignored)
├── normalized/                 Pre-projected parquet + gpkg (served to loads)
└── exports/                    Per-request exports (writable, TTL 24h)
docs/                           Markdown docs (public slugs + underscore-prefixed private)
.duckdb/
├── tmp/                        DuckDB temp-spill directory
└── sessions/                   Persistent session files (.duckdb + .meta.json)
```

At runtime, the sandbox restricts writes to `data/exports` and `.duckdb`;
everything else is read-only. Secrets live outside the repo tree and are
delivered through the credential manager at process start.

---

## Concurrency model

- One `asyncio`-driven uvicorn process.
- Each incoming request is handled asynchronously, but tool bodies are
  synchronous (they block in DuckDB C++).
- Sessions lock via DuckDB's internal mutex (one connection, many
  threads). Inside a single session, tool calls serialise.
- Across sessions, they parallelise to the extent uvicorn's worker can
  overlap.
- The session registry uses a Python `threading.Lock` for the
  registry-level dict + background GC thread.

This is not a high-QPS design. At current load (single-digit concurrent
users), it's simple and correct. Horizontal scaling would require
splitting sessions across workers, which we haven't needed.

---

## What's deliberately missing

- **No background job queue.** Everything runs in the request path. If a
  tool takes 30 seconds, the client waits 30 seconds. This keeps the
  error-flow simple — there's no "check back later" state machine.
- **No metrics / tracing backend.** `journalctl -u geodata-mcp -f` is the
  operations tool. At current scale this is fine.
- **No WebSocket push to the viewer.** Auto-refresh is HTTP polling of a
  version endpoint (every 2 seconds when the tab is visible). Simpler
  than WebSocket, good enough for a demo.
