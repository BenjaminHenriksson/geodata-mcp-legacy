"""Session manager — DuckDB connection + layer registry + operation log.

Multi-session: each MCP client connection gets its own session keyed by the
MCP Context's `session_id`. Idle sessions GC to disk after `IDLE_TIMEOUT_S`.
After GC, subsequent tool calls against that session id get a structured
`SessionExpired` error carrying a replay log so the caller can reconstruct
state if desired.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import duckdb

from .catalog import DatasetEntry

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
    }


def _op_from_dict(d: dict) -> Operation:
    return Operation(
        tool=d["tool"],
        args=d.get("args", {}) or {},
        result_layer=d.get("result_layer"),
        summary=d.get("summary", ""),
        at=_parse_dt(d.get("at")),
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


class Session:
    """Per-MCP-connection state. Each session has an on-disk DuckDB file and
    a JSON sidecar for Python-side metadata (layers, styles, history,
    checkpoints). Rehydrated from disk on server restart.

    A short URL-safe id is generated here; callers may pass in an
    MCP-provided session id to adopt it (for stable viewer URLs across
    reconnects).
    """

    def __init__(self, session_id: str | None = None) -> None:
        self.id = session_id or uuid.uuid4().hex[:12]
        self.created_at = datetime.utcnow()
        self.last_active = self.created_at
        SESSION_DB_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_DB_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        self._db_path = SESSION_DB_DIR / f"{self.id}.duckdb"
        self._meta_path = SESSION_DB_DIR / f"{self.id}.meta.json"
        self.conn = duckdb.connect(str(self._db_path))
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
        self.history.append(op)
        self.touch()
        # Any logged operation is by definition something the viewer might
        # want to reflect. Bumping here catches mutations that don't go
        # through register().
        self.version += 1
        self._dirty = True

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
            import sys
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
        s.layers = {n: _layer_from_dict(m)
                    for n, m in (data.get("layers") or {}).items()}
        s.visible_layers = list(data.get("visible_layers") or [])
        s.visible_styles = dict(data.get("visible_styles") or {})
        s.visible_title = data.get("visible_title")
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
        for p in (self._db_path, self._meta_path):
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
