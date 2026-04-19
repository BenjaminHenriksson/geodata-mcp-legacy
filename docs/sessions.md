# Sessions and persistence

A session is the unit of work. It has a unique id, its own DuckDB file,
its own set of layers, and survives restarts.

---

## Lifecycle

```
[absent]
   │
   │ first tool call with a new session_id
   ▼
[live]                    ← most tool calls land here
   │
   │ 30 min idle   (IDLE_TIMEOUT_S)
   ▼
[cold]                    ← files on disk, handle closed
   │       │
   │       │ next tool call or viewer hit
   │       ▼
   │    [live again] ——→ rehydrated from .duckdb + .meta.json
   │
   │ 14 days without a touch   (HARD_TTL_S)
   ▼
[deleted]                 ← files gone; next touch gets a fresh session
```

### Live

- DuckDB connection is open.
- Memory cap: 256 MB per connection.
- Thread cap: 2 threads per connection.
- All operations serialise at the DuckDB-connection level.

### Cold

- DuckDB connection closed, file still on disk.
- Every mutation has already flushed the sidecar (see "Dirty flush"
  below), so reopening is safe.
- GC loop periodically scans for sessions past the hard TTL and removes
  their files.

### Rehydrated

- Lazy: only happens when the session id is touched again.
- Opens the `.duckdb` file, loads the `spatial` extension, reads the
  sidecar, rebuilds `LayerMeta` / `visible_styles` / `history` /
  `checkpoints` from JSON.
- LLM's next tool call sees exactly the state it left behind.

---

## On-disk layout

```
.duckdb/sessions/
    <session_id>.duckdb           — tables: layers + checkpoint snapshots
    <session_id>.meta.json        — Python-side metadata
```

Two files per session. If either is deleted or corrupt, the other is
discarded — we don't try to rebuild a broken session. Recovery is the
LLM's job (via the history log that *was* saved).

### What's in `<id>.duckdb`

Every table the session has created:

- Layer tables (from `load`, `filter`, `spatial`, `execute_sql`, macros)
  with their geometry columns.
- `_snap_<checkpoint_id>_<layer>_<column>` tables — pre-image snapshots
  captured at checkpoint time, used by `rollback`.
- DuckDB's own catalog metadata (column types, constraints).

### What's in `<id>.meta.json`

Python-side state that DuckDB doesn't know about:

- `layers` — per-layer `LayerMeta` (feature count, geometry type, bbox,
  attributes dict, provenance SourceRefs, parent layers, notes, column
  provenance).
- `visible_layers`, `visible_styles`, `visible_title` — viewer state.
- `history` — every Operation logged, with tool name, args, summary,
  timestamp. Used for `sources(layer)` and for LLM recovery.
- `cursors` — state for `batch_iterate` pagination.
- `checkpoints` — checkpoint metadata: id, snapshot refs, scope. Note
  the actual snapshot *data* lives in DuckDB tables, not here.
- `next_checkpoint_id`, `active_checkpoint`, `version`.

---

## Dirty-flag flushing

Every mutation (`register`, `log`, `bump_version`) sets `self._dirty =
True`. The GC thread runs every 60 seconds and calls `s.persist()` on
every dirty session, then clears the flag.

This is a trade-off between write amplification and durability:

- **Every-mutation flush** would be safer (at most one mutation lost to
  a crash) but the sidecar is ~1-100 KB, so a session doing 20 rapid
  operations does 20 redundant disk writes.
- **Every-60-seconds flush** is what we do. Up to 60 seconds of mutations
  can be lost to a hard crash. A graceful service restart (SIGTERM) is
  handled via a wrapped lifespan that flushes *all* dirty sessions
  before exit.

If the process is killed ungracefully (SIGKILL, OOM) we lose up to a
minute of state.

---

## Graceful shutdown

The Starlette app wraps FastMCP's lifespan with an outer context manager:

```python
async with _inner_lifespan(app):
    try:
        yield
    finally:
        REGISTRY.flush_all()
```

`flush_all` walks every live session, calls `s.close()` (which flushes
then closes the handle), and empties the registry. This makes
graceful service restart safe — nothing is lost.

For ungraceful kill (`SIGKILL`, OOM, power loss), we lose up to one GC
interval. The 60-second cadence is tuned to this.

---

## Checkpoints and persistence

Checkpoints work across persistence:

- `checkpoint("x", layers=["a"])` records in-memory. The checkpoint
  metadata is part of the dirty-flush cycle, so it survives restart.
- Snapshot tables (`_snap_<id>_<layer>_<col>`) are created in DuckDB at
  mutation time. They persist to disk automatically because they're
  just tables in the DB file.
- After restart, `s.checkpoints` rebuilds from the sidecar. Snapshot
  tables are already there. `rollback("x")` executes the same SQL path
  as before.

Tested end-to-end: checkpoint → mutate → restart → rollback reverses the
mutation correctly.

---

## `get_or_create` semantics

```
get_or_create(session_id=None)
    if session_id in live-registry:
        return it
    if sidecar exists on disk:
        rehydrate into registry
        return it
    if sidecar is missing or corrupt:
        create fresh (possibly reusing the requested id, overwriting
        any orphaned .duckdb file)
    else:
        create brand new with fresh uuid
```

A session id from the MCP context is always honoured (stable URL across
reconnects). An id with no disk trace means the LLM has a fresh
workspace.

The only "you've lost your session" case is: the session's files were
hard-TTL-deleted (14 days without a touch), and the LLM tries to use
the same id. Returns a fresh session with the same id — the LLM may be
momentarily confused but adapts.

A more pedantic implementation would raise `SessionExpired` in this
case. We chose not to because the UX of "start over" is the same either
way and the exception path adds no recoverable information.

---

## Memory discipline

Each DuckDB connection is capped:

- `SET memory_limit = '256MB'` — a greedy ORDER BY or hash join that
  would blow past this instead spills to
  `.duckdb/tmp/<pid>-<hash>.tmp`. Degraded but correct.
- `SET threads = 2` — keeps a single session from consuming all CPU.
- `SET temp_directory = '.duckdb/tmp'` — keeps spill inside
  `ReadWritePaths`.

The memory cap is per-connection, not per-process, so ten concurrent
sessions can each use 256 MB. On a 4 GB VPS that's workable. If we ever
run into pressure, raising `memory_limit` is the wrong move — spilling
is the right move, because a well-behaved session doesn't actually
need much RAM.

---

## Ids are ephemeral but recoverable

Session ids are 12-char hex. They're generated server-side unless the
MCP client supplies one (via `Context.session_id`). The viewer URL is
`/view/<session_id>`, so sharing a session means sharing a link.

A 48-bit id space means collisions are astronomically unlikely at
current scale. If we ever scale to millions of sessions it'd be worth
widening; for now the shorter form reads better in URLs and logs.

---

## What sessions do *not* hold

- **User identity.** We don't know who opened the session. The OAuth
  flow issues a token for the MCP endpoint, but the session itself has
  no user record.
- **Per-user history.** A user with ten sessions has ten independent
  workspaces. There's no aggregated "all my work" view.
- **Access control between sessions.** A session id is the only
  authentication needed to reach a session's viewer + API endpoints.
  This is acceptable because everything in sessions is derived from
  open data.

If the system ever hosts non-open data, sessions would need to carry
a per-user ACL. Not done today.
