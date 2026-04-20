"""Session manager — DuckDB connection + layer registry + operation log.

Multi-session: each MCP client connection gets its own session keyed by the
MCP Context's `session_id`. Idle sessions GC to disk after `IDLE_TIMEOUT_S`.
After GC, subsequent tool calls against that session id get a structured
`SessionExpired` error carrying a replay log so the caller can reconstruct
state if desired.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import duckdb

from .catalog import DatasetEntry

# Per-tool-call audit context. Set by Session.audit_context(), read by
# _AuditedConnection.execute() and Session.log() so every SQL statement and
# every Operation logged inside a tool inherits the same correlation_id and
# user-facing description. None outside any tool call (e.g. startup probes,
# GC paths) — those audit records are tagged internal=True.
_audit_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "geodata_audit_ctx", default=None,
)

IDLE_TIMEOUT_S = 30 * 60           # 30 min idle → close handle, keep files
GC_INTERVAL_S = 60                  # check every minute
# Files on disk past this without a touch are deleted outright. Gives the
# LLM / user plenty of runway to come back to a paused analysis, while
# still bounding disk usage for abandoned sessions.
HARD_TTL_S = 14 * 24 * 60 * 60     # 14 days → hard delete
SESSION_LOG_DIR = Path("/tmp/geodata_sessions")
SESSION_DB_MEMORY_LIMIT = "256MB"
SESSION_DB_THREADS = 2
# DuckDB spills to disk when a query exceeds memory_limit. It defaults to
# creating `.tmp/` in the process cwd, which under systemd sandbox is the
# project root (read-only). Pin it to a path inside ReadWritePaths so
# large ORDER BY / hash-join / aggregate queries don't fail with
# "Failed to create directory '.tmp'".
SESSION_DB_TEMP_DIR = Path(__file__).resolve().parents[1] / ".duckdb" / "tmp"
# Persistent session storage. Each session gets `<id>.duckdb` (the tables)
# plus `<id>.meta.json` (Python-side state: LayerMeta, styles, history,
# checkpoints). Rehydrated on startup.
SESSION_DB_DIR = Path(__file__).resolve().parents[1] / ".duckdb" / "sessions"


@dataclass
class SourceRef:
    dataset_id: str | None
    source_name: str
    publisher: str
    license: str
    url: str | None
    file_path: str | None
    retrieved: str
    llm_sourced: bool = False
    llm_source_description: str | None = None


@dataclass
class LayerMeta:
    name: str
    feature_count: int
    geometry_type: str | None
    bbox: tuple[float, float, float, float] | None  # EPSG:3011 (xmin, ymin, xmax, ymax)
    attributes: dict[str, str]                       # column → type
    created_by: str
    created_at: datetime
    provenance: list[SourceRef]
    parent_layers: list[str] = field(default_factory=list)
    notes: str = ""
    # Per-column provenance: {column_name: {"authored_by": "llm"|"derived",
    # "tool": str, "at": iso_ts, "model": str|None}}.
    # Populated by annotate/add_field/update_field so an exported column can
    # be traced to "LLM-written on date X by tool Y". Columns absent from
    # this dict inherit the layer's overall provenance.
    column_provenance: dict[str, dict] = field(default_factory=dict)


@dataclass
class Operation:
    tool: str
    args: dict
    result_layer: str | None
    summary: str
    at: datetime
    # Audit fields — populated automatically by Session.log() from the active
    # audit context, then patched with status/duration when the context exits.
    # Defaults keep backward-compat for callers that build Operation without
    # them (and for sidecar JSON written before this field existed).
    correlation_id: str | None = None
    description: str = ""
    status: str = "ok"           # "ok" | "error"
    duration_ms: int | None = None
    error: str | None = None


@dataclass
class AuditRecord:
    """Single SQL statement captured by _AuditedConnection. Every conn.execute()
    produces one of these. Tagged with the active tool's correlation_id when
    inside a Session.audit_context, else internal=True."""
    correlation_id: str | None
    tool: str
    sql: str
    started_at: datetime
    duration_ms: int
    status: str                  # "ok" | "error"
    error_type: str | None = None
    error: str | None = None
    internal: bool = False


class SessionExpired(Exception):
    """Raised when a tool call targets a session id whose DuckDB state has
    already been GCed. `replay_info` contains the persisted operation log so
    the caller may reconstruct the session."""

    def __init__(self, session_id: str, replay_info: dict) -> None:
        super().__init__(
            f"Session {session_id} expired after {IDLE_TIMEOUT_S//60} min idle. "
            f"Replay info attached ({len(replay_info.get('operations', []))} ops)."
        )
        self.session_id = session_id
        self.replay_info = replay_info


def _op_to_dict(op: Operation) -> dict:
    return {
        "tool": op.tool,
        "args": op.args,
        "result_layer": op.result_layer,
        "summary": op.summary,
        "at": op.at.isoformat() + "Z",
        "correlation_id": op.correlation_id,
        "description": op.description,
        "status": op.status,
        "duration_ms": op.duration_ms,
        "error": op.error,
    }


def _op_from_dict(d: dict) -> Operation:
    return Operation(
        tool=d["tool"],
        args=d.get("args", {}) or {},
        result_layer=d.get("result_layer"),
        summary=d.get("summary", ""),
        at=_parse_dt(d.get("at")),
        correlation_id=d.get("correlation_id"),
        description=d.get("description", "") or "",
        status=d.get("status", "ok") or "ok",
        duration_ms=d.get("duration_ms"),
        error=d.get("error"),
    )


def _audit_to_dict(rec: AuditRecord) -> dict:
    return {
        "correlation_id": rec.correlation_id,
        "tool": rec.tool,
        "sql": rec.sql,
        "started_at": rec.started_at.isoformat() + "Z",
        "duration_ms": rec.duration_ms,
        "status": rec.status,
        "error_type": rec.error_type,
        "error": rec.error,
        "internal": rec.internal,
    }


def _audit_from_dict(d: dict) -> AuditRecord:
    return AuditRecord(
        correlation_id=d.get("correlation_id"),
        tool=d.get("tool", ""),
        sql=d.get("sql", ""),
        started_at=_parse_dt(d.get("started_at")),
        duration_ms=int(d.get("duration_ms", 0)),
        status=d.get("status", "ok") or "ok",
        error_type=d.get("error_type"),
        error=d.get("error"),
        internal=bool(d.get("internal", False)),
    )


def _source_to_dict(s: SourceRef) -> dict:
    return {
        "dataset_id": s.dataset_id, "source_name": s.source_name,
        "publisher": s.publisher, "license": s.license, "url": s.url,
        "file_path": s.file_path, "retrieved": s.retrieved,
        "llm_sourced": s.llm_sourced,
        "llm_source_description": s.llm_source_description,
    }


def _source_from_dict(d: dict) -> SourceRef:
    return SourceRef(
        dataset_id=d.get("dataset_id"),
        source_name=d.get("source_name", ""),
        publisher=d.get("publisher", ""),
        license=d.get("license", ""),
        url=d.get("url"),
        file_path=d.get("file_path"),
        retrieved=d.get("retrieved", ""),
        llm_sourced=bool(d.get("llm_sourced", False)),
        llm_source_description=d.get("llm_source_description"),
    )


def _layer_to_dict(m: LayerMeta) -> dict:
    return {
        "name": m.name,
        "feature_count": m.feature_count,
        "geometry_type": m.geometry_type,
        "bbox": list(m.bbox) if m.bbox else None,
        "attributes": dict(m.attributes),
        "created_by": m.created_by,
        "created_at": m.created_at.isoformat() + "Z",
        "provenance": [_source_to_dict(s) for s in m.provenance],
        "parent_layers": list(m.parent_layers),
        "notes": m.notes,
        "column_provenance": dict(m.column_provenance),
    }


def _layer_from_dict(d: dict) -> LayerMeta:
    bbox = d.get("bbox")
    return LayerMeta(
        name=d["name"],
        feature_count=int(d.get("feature_count", 0)),
        geometry_type=d.get("geometry_type"),
        bbox=tuple(bbox) if bbox else None,
        attributes=dict(d.get("attributes", {})),
        created_by=d.get("created_by", ""),
        created_at=_parse_dt(d.get("created_at")),
        provenance=[_source_from_dict(s) for s in d.get("provenance", [])],
        parent_layers=list(d.get("parent_layers", [])),
        notes=d.get("notes", ""),
        column_provenance=dict(d.get("column_provenance", {})),
    )


def _parse_dt(s: str | None) -> datetime:
    if not s:
        return datetime.utcnow()
    s = s.rstrip("Z")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.utcnow()


class _AuditedConnection:
    """Thin proxy around a DuckDBPyConnection that records every execute().

    Forwards every other attribute (.description, .interrupt, .close, etc.)
    via __getattr__, so callers see a normal connection. Only execute() is
    intercepted: timed, captured, and tagged with the active audit context.
    Calls outside any audit_context are tagged internal=True with tool
    "<probe>" — covers the startup `INSTALL spatial`, `SET memory_limit`
    probes inside operations.py (DESCRIBE, COUNT, bbox), GC paths, etc.
    """

    __slots__ = ("_raw", "_session")

    def __init__(self, raw_conn, session: "Session") -> None:
        # __slots__ + object.__setattr__ avoids any chance of recursion via
        # a future __setattr__ override.
        object.__setattr__(self, "_raw", raw_conn)
        object.__setattr__(self, "_session", session)

    def execute(self, sql, *args, **kwargs):
        ctx = _audit_ctx.get()
        started = time.monotonic()
        started_at = datetime.utcnow()
        status = "ok"
        error_type: str | None = None
        error_msg: str | None = None
        try:
            return self._raw.execute(sql, *args, **kwargs)
        except Exception as e:
            status = "error"
            error_type = type(e).__name__
            error_msg = str(e)
            raise
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            try:
                self._session._record_audit(AuditRecord(
                    correlation_id=(ctx.get("correlation_id") if ctx else None),
                    tool=(ctx.get("tool") if ctx else "<probe>"),
                    sql=sql if isinstance(sql, str) else repr(sql),
                    started_at=started_at,
                    duration_ms=duration_ms,
                    status=status,
                    error_type=error_type,
                    error=error_msg,
                    internal=(ctx is None),
                ))
            except Exception as audit_err:
                # Never let audit bookkeeping mask the real query result.
                print(f"[session {self._session.id}] audit record failed: "
                      f"{audit_err!r}", file=sys.stderr)

    def __getattr__(self, name):
        # Called only when an attribute is NOT found on the proxy itself.
        # Forward to the wrapped DuckDBPyConnection.
        return getattr(self._raw, name)


class Session:
    """Per-MCP-connection state. Each session has an on-disk DuckDB file and
    a JSON sidecar for Python-side metadata (layers, styles, history,
    checkpoints). Rehydrated from disk on server restart.

    A short URL-safe id is generated here; callers may pass in an
    MCP-provided session id to adopt it (for stable viewer URLs across
    reconnects).
    """

    def __init__(self, session_id: str | None = None) -> None:
        # 128 bits of entropy via secrets.token_urlsafe(16) → 22 URL-safe
        # chars (a-z A-Z 0-9 - _). Earlier versions used uuid4.hex[:12]
        # (48 bits) which was brute-forceable: the viewer API is gated
        # only by the session id, so enumeration equals auth bypass.
        self.id = session_id or secrets.token_urlsafe(16)
        self.created_at = datetime.utcnow()
        self.last_active = self.created_at
        SESSION_DB_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_DB_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self._db_path = SESSION_DB_DIR / f"{self.id}.duckdb"
        self._meta_path = SESSION_DB_DIR / f"{self.id}.meta.json"
        self._audit_path = SESSION_DB_DIR / f"{self.id}.audit.jsonl"
        # Append-only timeline of every SQL statement (incl. internal probes).
        # Mirrored line-by-line to _audit_path so it survives restarts.
        self.audit: list[AuditRecord] = []
        # Wrap the raw connection in an audit proxy BEFORE any execute() —
        # the very first INSTALL spatial / SET pragma calls below get logged
        # as internal records, which is what we want for full traceability.
        _raw_conn = duckdb.connect(str(self._db_path))
        self.conn = _AuditedConnection(_raw_conn, self)
        self.conn.execute("INSTALL spatial; LOAD spatial;")
        # Cap per-session resource usage so one greedy SQL can't OOM others.
        self.conn.execute(f"SET memory_limit = '{SESSION_DB_MEMORY_LIMIT}'")
        self.conn.execute(f"SET threads = {SESSION_DB_THREADS}")
        self.conn.execute(f"SET temp_directory = '{SESSION_DB_TEMP_DIR}'")
        self.layers: dict[str, LayerMeta] = {}
        self.visible_layers: list[str] = []
        # Optional per-layer style spec consumed by the viewer. Shape:
        #   {layer_name: {"column": str, "scale": "categorical"|"linear",
        #                 "palette": dict|list, "size"|"opacity"|"stroke": {...}}}
        # Set by the show() tool when the caller passes `style=...`.
        self.visible_styles: dict[str, dict] = {}
        # Optional viewer title set via show(title=...). Rendered in the
        # viewer's panel header and <title>; falls back to a default.
        self.visible_title: str | None = None
        self.history: list[Operation] = []
        # Cursors for batch_iterate — cursor_id → iteration state.
        self.cursors: dict[str, dict] = {}
        # Most recently created checkpoint name (for hint text). Multiple may
        # be active concurrently — see `checkpoints`.
        self.active_checkpoint: str | None = None
        # name → {"id": int, "snapshots": [...], "layers": set[str]|None}
        # `layers=None` means the checkpoint covers every layer. A mutation
        # snapshots against every active checkpoint whose scope covers its
        # layer.
        self.checkpoints: dict[str, dict] = {}
        self._next_checkpoint_id = 0
        # Monotonic counter bumped on any change that matters to the viewer —
        # load/filter/spatial/execute_sql creating layers, show/hide, mutations.
        # The viewer polls /api/<sid>/visible_layers and diffs on this to know
        # when to re-fetch GeoJSON.
        self.version = 0
        # Dirty flag: set on any mutation, cleared on persist. The GC thread
        # periodically flushes dirty sessions so we aren't writing the
        # sidecar on every single version bump (cheap, but still avoidable).
        self._dirty = False

    def bump_version(self) -> None:
        self.version += 1
        self._dirty = True

    def touch(self) -> None:
        self.last_active = datetime.utcnow()

    def register(self, meta: LayerMeta) -> None:
        self.layers[meta.name] = meta
        self.version += 1
        self._dirty = True

    def log(self, op: Operation) -> None:
        # Inherit correlation_id + description from the active audit context
        # so callers in operations.py don't have to pass them explicitly.
        ctx = _audit_ctx.get()
        if ctx is not None:
            if op.correlation_id is None:
                op.correlation_id = ctx.get("correlation_id")
            if not op.description:
                op.description = ctx.get("description", "") or ""
        self.history.append(op)
        self.touch()
        # Any logged operation is by definition something the viewer might
        # want to reflect. Bumping here catches mutations that don't go
        # through register().
        self.version += 1
        self._dirty = True

    @contextlib.contextmanager
    def audit_context(self, tool: str, description: str = "",
                      args: dict | None = None):
        """Open a per-tool-call audit scope. Every SQL run inside (via the
        proxied conn) and every Operation logged inherits a fresh
        `correlation_id`. On exit, status/duration_ms are patched onto every
        Operation that was logged under this id; if the body logged nothing
        (e.g. read-only tools), a synthetic Operation is added so the audit
        feed still shows the call.
        """
        cid = secrets.token_urlsafe(8)
        started = time.monotonic()
        started_at = datetime.utcnow()
        token = _audit_ctx.set({
            "correlation_id": cid,
            "tool": tool,
            "description": description or "",
            "args": args or {},
        })
        status = "ok"
        error_str: str | None = None
        try:
            yield cid
        except Exception as e:
            status = "error"
            error_str = f"{type(e).__name__}: {e}"
            raise
        finally:
            _audit_ctx.reset(token)
            duration_ms = int((time.monotonic() - started) * 1000)
            patched = False
            for op in self.history:
                if op.correlation_id == cid:
                    op.status = status
                    op.duration_ms = duration_ms
                    if status == "error" and not op.error:
                        op.error = error_str
                    patched = True
            if not patched:
                # Read-only tool, or the body raised before any session.log().
                # Synthesize an Operation so the audit panel still surfaces it.
                self.log(Operation(
                    tool=tool, args=args or {}, result_layer=None,
                    summary="" if status == "ok" else (error_str or "error"),
                    at=started_at,
                    correlation_id=cid, description=description or "",
                    status=status, duration_ms=duration_ms, error=error_str,
                ))
            self._dirty = True

    def _record_audit(self, rec: AuditRecord) -> None:
        """Append an AuditRecord to the in-memory list and the JSONL sidecar.
        Called from _AuditedConnection.execute() — must never raise."""
        self.audit.append(rec)
        try:
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(_audit_to_dict(rec), ensure_ascii=False)
                        + "\n")
        except OSError as e:
            print(f"[session {self.id}] audit append failed: {e!r}",
                  file=sys.stderr)

    def unique_layer_name(self, base: str) -> str:
        if base not in self.layers:
            return base
        i = 2
        while f"{base}_{i}" in self.layers:
            i += 1
        return f"{base}_{i}"

    def persist_log(self) -> dict:
        """Dump a serializable replay log. Used for LLM-facing replay hints."""
        return {
            "session_id": self.id,
            "created_at": self.created_at.isoformat() + "Z",
            "gc_at": datetime.utcnow().isoformat() + "Z",
            "final_layers": list(self.layers.keys()),
            "operations": [_op_to_dict(op) for op in self.history],
        }

    def state_dict(self) -> dict:
        """Full Python-side state for the sidecar JSON. DuckDB data
        (layer tables, checkpoint snapshot tables) lives in the .duckdb
        file and is not duplicated here."""
        ckpts = {}
        for name, c in self.checkpoints.items():
            scope = c.get("layers")
            ckpts[name] = {
                "id": c.get("id"),
                "snapshots": list(c.get("snapshots", [])),
                "layers": None if scope is None else sorted(scope),
            }
        return {
            "schema_version": 1,
            "id": self.id,
            "created_at": self.created_at.isoformat() + "Z",
            "last_active": self.last_active.isoformat() + "Z",
            "version": self.version,
            "layers": {n: _layer_to_dict(m) for n, m in self.layers.items()},
            "visible_layers": list(self.visible_layers),
            "visible_styles": dict(self.visible_styles),
            "visible_title": self.visible_title,
            "history": [_op_to_dict(op) for op in self.history],
            "cursors": dict(self.cursors),
            "active_checkpoint": self.active_checkpoint,
            "checkpoints": ckpts,
            "next_checkpoint_id": self._next_checkpoint_id,
        }

    def persist(self) -> None:
        """Atomically write the sidecar JSON. Safe to call frequently."""
        try:
            tmp = self._meta_path.with_suffix(".meta.tmp")
            tmp.write_text(
                json.dumps(self.state_dict(), ensure_ascii=False,
                           indent=None, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(self._meta_path)
            self._dirty = False
        except Exception as e:
            # Never let a persist failure break a tool call. Re-dirty so
            # the next flush will retry.
            self._dirty = True
            print(f"[session {self.id}] persist failed: {e!r}", file=sys.stderr)

    @classmethod
    def rehydrate(cls, meta_path: Path) -> "Session | None":
        """Reopen a session from disk. Returns None if the files are
        missing or corrupt."""
        if not meta_path.exists():
            return None
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        sid = data.get("id")
        if not sid:
            return None
        db_path = SESSION_DB_DIR / f"{sid}.duckdb"
        if not db_path.exists():
            return None
        s = cls(session_id=sid)
        # __init__ opened the DB and set created_at=now; overwrite with the
        # saved values so provenance timestamps and GC logic stay honest.
        s.created_at = _parse_dt(data.get("created_at"))
        s.last_active = _parse_dt(data.get("last_active"))
        s.version = int(data.get("version", 0))
        # Self-heal any legacy free-text fields that were stored with
        # literal \uXXXX escape sequences (an over-escape bug in some MCP
        # client paths). Decode on the way out so the viewer / API see
        # real characters.
        from .operations import decode_unicode_escapes as _dec
        s.layers = {n: _layer_from_dict(m)
                    for n, m in (data.get("layers") or {}).items()}
        for _m in s.layers.values():
            _m.notes = _dec(_m.notes) if _m.notes else _m.notes
        s.visible_layers = list(data.get("visible_layers") or [])
        s.visible_styles = dict(data.get("visible_styles") or {})
        _vt = data.get("visible_title")
        s.visible_title = _dec(_vt) if _vt else _vt
        s.history = [_op_from_dict(op) for op in (data.get("history") or [])]
        s.cursors = dict(data.get("cursors") or {})
        s.active_checkpoint = data.get("active_checkpoint")
        s._next_checkpoint_id = int(data.get("next_checkpoint_id", 0))
        ckpts = {}
        for name, c in (data.get("checkpoints") or {}).items():
            scope = c.get("layers")
            ckpts[name] = {
                "id": c.get("id"),
                "snapshots": list(c.get("snapshots", [])),
                "layers": None if scope is None else set(scope),
            }
        s.checkpoints = ckpts
        # Replay the audit JSONL into memory so the viewer can show history
        # across restarts. JSONL is append-only on disk; in memory we just
        # rebuild the list. Skip startup probes that ran during this very
        # rehydrate (those came from the *new* connection and are already in
        # s.audit at this point) — they live after the file's last line.
        try:
            audit_path = SESSION_DB_DIR / f"{sid}.audit.jsonl"
            if audit_path.exists():
                replayed: list[AuditRecord] = []
                with audit_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            replayed.append(_audit_from_dict(json.loads(line)))
                        except Exception:
                            continue
                # Prepend historical records before the just-recorded
                # rehydrate-time probes.
                s.audit = replayed + s.audit
        except OSError as e:
            print(f"[session {sid}] audit replay failed: {e!r}",
                  file=sys.stderr)
        s._dirty = False
        return s

    def close(self) -> None:
        """Flush state and release the DuckDB handle. File stays on disk."""
        if self._dirty:
            self.persist()
        try:
            self.conn.close()
        except Exception:
            pass

    def delete_files(self) -> None:
        """Remove persistent files — session is unrecoverable after this."""
        for p in (self._db_path, self._meta_path, self._audit_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def source_from_dataset(d: DatasetEntry) -> SourceRef:
        return SourceRef(
            dataset_id=d.id, source_name=d.name_sv,
            publisher=d.publisher, license=d.license,
            url=d.source_url, file_path=d.file_path,
            retrieved=d.retrieved,
        )


class SessionRegistry:
    """In-process registry with background GC + disk persistence.

    State machine for a given session id:
      - absent  → create-on-touch (live, DuckDB handle open)
      - live    → idle N min → handle closed, files on disk
      - cold    → next touch → handle reopened, rehydrated from sidecar
      - cold    → last_active older than HARD_TTL_S → files deleted
      - truly-gone → next touch raises SessionExpired (files deleted)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_DB_DIR.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self._gc_thread: threading.Thread | None = None

    def start_gc(self) -> None:
        """Call once at server startup to begin the idle-GC loop."""
        if self._gc_thread is not None:
            return
        self._gc_thread = threading.Thread(
            target=self._gc_loop, name="session-gc", daemon=True,
        )
        self._gc_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.flush_all()

    def flush_all(self) -> None:
        """Persist every live session's sidecar and close its handle.
        Called on shutdown so a SIGTERM doesn't lose dirty state."""
        with self._lock:
            for s in list(self._sessions.values()):
                try:
                    s.close()
                except Exception:
                    pass
            self._sessions.clear()

    def _meta_path(self, sid: str) -> Path:
        return SESSION_DB_DIR / f"{sid}.meta.json"

    def _db_path(self, sid: str) -> Path:
        return SESSION_DB_DIR / f"{sid}.duckdb"

    def _try_rehydrate(self, sid: str) -> Session | None:
        """Open an on-disk session into memory. Caller must hold the lock."""
        s = Session.rehydrate(self._meta_path(sid))
        if s is not None:
            s.touch()
            self._sessions[sid] = s
        return s

    def get_or_create(self, session_id: str | None = None) -> Session:
        with self._lock:
            if session_id and session_id in self._sessions:
                s = self._sessions[session_id]
                s.touch()
                return s
            # Not in memory — try disk.
            if session_id and self._meta_path(session_id).exists():
                s = self._try_rehydrate(session_id)
                if s is not None:
                    return s
                # Sidecar was corrupt. Fall through to a fresh session
                # with the requested id (drops the old DuckDB file if any).
                self._db_path(session_id).unlink(missing_ok=True)
            # Brand new session.
            s = Session(session_id)
            self._sessions[s.id] = s
            s.persist()
            return s

    def get(self, session_id: str) -> Session | None:
        """Look up a session without creating one (for viewer endpoints).
        Rehydrates from disk if needed."""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is not None:
                s.touch()
                return s
            if self._meta_path(session_id).exists():
                return self._try_rehydrate(session_id)
            return None

    def all(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())

    def _gc_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(GC_INTERVAL_S)
            if self._stop.is_set():
                break
            try:
                self._sweep_once()
            except Exception:
                # The GC loop must never die — log via print at minimum.
                import traceback
                traceback.print_exc()

    def _sweep_once(self) -> None:
        now_dt = datetime.utcnow()
        # 1) Flush any dirty live sessions so restart loses nothing.
        # 2) Close handles for idle sessions (files stay on disk).
        with self._lock:
            for s in list(self._sessions.values()):
                if s._dirty:
                    s.persist()
                if (now_dt - s.last_active).total_seconds() > IDLE_TIMEOUT_S:
                    s.close()
                    self._sessions.pop(s.id, None)
        # 3) Hard-delete files older than HARD_TTL_S. Done outside the
        # lock since it only touches disk for already-cold sessions.
        cutoff = now_dt.timestamp() - HARD_TTL_S
        try:
            for meta_file in SESSION_DB_DIR.glob("*.meta.json"):
                sid = meta_file.name[: -len(".meta.json")]
                if sid in self._sessions:
                    continue
                try:
                    mtime = meta_file.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    meta_file.unlink(missing_ok=True)
                    (SESSION_DB_DIR / f"{sid}.duckdb").unlink(missing_ok=True)
        except FileNotFoundError:
            pass


REGISTRY = SessionRegistry()
