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

IDLE_TIMEOUT_S = 30 * 60           # 30 min idle → GC
GC_INTERVAL_S = 60                  # check every minute
EXPIRED_RETAIN_S = 24 * 60 * 60    # remember expired sessions' logs for 24h
SESSION_LOG_DIR = Path("/tmp/geodata_sessions")
SESSION_DB_MEMORY_LIMIT = "256MB"
SESSION_DB_THREADS = 2


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


class Session:
    """Per-MCP-connection state. One DuckDB in-memory database, one layer namespace.
    A short URL-safe id is generated here; callers may pass in an MCP-provided
    session id to adopt it (for stable viewer URLs across reconnects)."""

    def __init__(self, session_id: str | None = None) -> None:
        self.id = session_id or uuid.uuid4().hex[:12]
        self.created_at = datetime.utcnow()
        self.last_active = self.created_at
        self.conn = duckdb.connect(":memory:")
        self.conn.execute("INSTALL spatial; LOAD spatial;")
        # Cap per-session resource usage so one greedy SQL can't OOM others.
        self.conn.execute(f"SET memory_limit = '{SESSION_DB_MEMORY_LIMIT}'")
        self.conn.execute(f"SET threads = {SESSION_DB_THREADS}")
        self.layers: dict[str, LayerMeta] = {}
        self.visible_layers: list[str] = []
        self.history: list[Operation] = []
        # Cursors for batch_iterate — cursor_id → iteration state.
        self.cursors: dict[str, dict] = {}
        # Active checkpoint (one at a time). When set, in-place mutations
        # snapshot the affected column(s) before writing.
        self.active_checkpoint: str | None = None
        # name → {"id": int, "snapshots": [{"layer": ..., "column": ..., "snap_table": ...,
        #                                   "column_existed": bool, "whole_layer": bool}]}
        self.checkpoints: dict[str, dict] = {}
        self._next_checkpoint_id = 0

    def touch(self) -> None:
        self.last_active = datetime.utcnow()

    def register(self, meta: LayerMeta) -> None:
        self.layers[meta.name] = meta

    def log(self, op: Operation) -> None:
        self.history.append(op)
        self.touch()

    def unique_layer_name(self, base: str) -> str:
        if base not in self.layers:
            return base
        i = 2
        while f"{base}_{i}" in self.layers:
            i += 1
        return f"{base}_{i}"

    def persist_log(self) -> dict:
        """Dump a serializable replay log. Used at GC time."""
        return {
            "session_id": self.id,
            "created_at": self.created_at.isoformat() + "Z",
            "gc_at": datetime.utcnow().isoformat() + "Z",
            "final_layers": list(self.layers.keys()),
            "operations": [_op_to_dict(op) for op in self.history],
        }

    @staticmethod
    def source_from_dataset(d: DatasetEntry) -> SourceRef:
        return SourceRef(
            dataset_id=d.id, source_name=d.name_sv,
            publisher=d.publisher, license=d.license,
            url=d.source_url, file_path=d.file_path,
            retrieved=d.retrieved,
        )


class SessionRegistry:
    """In-process registry with background GC.

    State machine for a given session id:
      - absent  → create-on-touch (live)
      - live    → idle N min → dumped to disk, removed from registry → expired
      - expired → next touch raises SessionExpired carrying the replay log
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._expired_at: dict[str, float] = {}  # id → expiry epoch
        SESSION_LOG_DIR.mkdir(parents=True, exist_ok=True)
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

    def get_or_create(self, session_id: str | None = None) -> Session:
        with self._lock:
            if session_id and session_id in self._sessions:
                s = self._sessions[session_id]
                s.touch()
                return s
            if session_id and session_id in self._expired_at:
                # Session was GCed — surface the replay log so the caller can choose.
                log_path = SESSION_LOG_DIR / f"{session_id}.json"
                replay = {}
                if log_path.exists():
                    try:
                        replay = json.loads(log_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                raise SessionExpired(session_id, replay)
            s = Session(session_id)
            self._sessions[s.id] = s
            return s

    def get(self, session_id: str) -> Session | None:
        """Look up a live session without creating one. Used by viewer API."""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is not None:
                s.touch()
            return s

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
        now = time.time()
        with self._lock:
            to_expire = [
                s for s in self._sessions.values()
                if (now_dt - s.last_active).total_seconds() > IDLE_TIMEOUT_S
            ]
            for s in to_expire:
                log = s.persist_log()
                path = SESSION_LOG_DIR / f"{s.id}.json"
                path.write_text(json.dumps(log, indent=2, ensure_ascii=False))
                try:
                    s.conn.close()
                except Exception:
                    pass
                self._sessions.pop(s.id, None)
                self._expired_at[s.id] = now + EXPIRED_RETAIN_S
            # forget old expirations and their log files
            for sid, expiry in list(self._expired_at.items()):
                if now > expiry:
                    self._expired_at.pop(sid, None)
                    path = SESSION_LOG_DIR / f"{sid}.json"
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass


REGISTRY = SessionRegistry()
