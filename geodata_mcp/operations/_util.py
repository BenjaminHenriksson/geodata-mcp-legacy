"""Shared helpers for the operations package.

Anything used from two or more operations submodules (or by the public
API itself) lives here. Submodules import narrowly from ``._util`` rather
than from each other so the dependency graph stays acyclic.
"""
from __future__ import annotations

import re
from datetime import datetime

import sqlglot

from ..loader import (
    LoadError as OpError,
    _bbox_from_table,
    _column_schema,
    _detect_geometry_column,
    _quote_ident,
)
from ..session import LayerMeta, Session, SourceRef


# Strict CRS whitelist — `create_layer` interpolates this value directly
# into a SQL literal that reaches DuckDB outside the execute_sql sandbox,
# so anything broader than EPSG:<digits> would reopen a full SQL injection
# → filesystem / httpfs / extension-loader reach.
_EPSG_RE = re.compile(r"EPSG:\d{4,6}")


# Some MCP clients / LLMs over-escape unicode when they build the JSON for
# a tool call — e.g. `title="Caféer"` arrives as the 13-character literal
# string `Caf\u00e9er` instead of the 7-character decoded form. We normalise
# at every free-text ingress so the viewer / docs / popups render real
# characters rather than literal `\uXXXX` sequences. No-op for strings that
# don't contain a backslash-u; safe to apply repeatedly.
#
# Security notes:
# - Every display surface downstream escapes HTML (textContent or
#   escapeHtml), so producing `<`, `>`, etc. via decode cannot introduce
#   XSS in the current code path.
# - Lone UTF-16 surrogates (U+D800..U+DFFF) cannot encode to UTF-8 and
#   would crash JSON/HTTP serialisation for the viewer API. We only
#   decode matched surrogate pairs and leave any remaining lone
#   surrogates as literal `\uXXXX` text.
# - Regex is linear-time; result is strictly shorter than input.
_UESC_PAIR_RE = re.compile(
    r"\\u([dD][89aAbB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})"
)
_UESC_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _join_surrogates(m: "re.Match[str]") -> str:
    hi = int(m.group(1), 16)
    lo = int(m.group(2), 16)
    return chr(0x10000 + (hi - 0xD800) * 0x400 + (lo - 0xDC00))


def _decode_single(m: "re.Match[str]") -> str:
    cp = int(m.group(1), 16)
    # Leave lone surrogates as the original literal text — chr() would
    # produce an un-UTF-8-encodable string and DoS the viewer API.
    if 0xD800 <= cp <= 0xDFFF:
        return m.group(0)
    return chr(cp)


def decode_unicode_escapes(s):
    """Replace literal ``\\uXXXX`` escapes in `s` with their characters.
    Accepts any value; returns non-strings unchanged. Handles surrogate
    pairs; leaves lone surrogates as literal text."""
    if not isinstance(s, str) or "\\u" not in s:
        return s
    try:
        s = _UESC_PAIR_RE.sub(_join_surrogates, s)
        return _UESC_RE.sub(_decode_single, s)
    except ValueError:
        return s


def decode_escapes_deep(v):
    """Recursively normalise every string value inside a dict/list/tuple.
    Used for annotate payloads where the LLM may have escaped any of the
    string values it wrote."""
    if isinstance(v, str):
        return decode_unicode_escapes(v)
    if isinstance(v, dict):
        return {k: decode_escapes_deep(x) for k, x in v.items()}
    if isinstance(v, list):
        return [decode_escapes_deep(x) for x in v]
    if isinstance(v, tuple):
        return tuple(decode_escapes_deep(x) for x in v)
    return v


# ---------- provenance / meta helpers ----------


def _merge_provenance(*layers: LayerMeta) -> list[SourceRef]:
    seen: dict[tuple, SourceRef] = {}
    for m in layers:
        for s in m.provenance:
            key = (s.dataset_id, s.source_name, s.file_path, s.llm_sourced)
            seen.setdefault(key, s)
    return list(seen.values())


def _probe_geom_type(session: Session, table: str, geom_col: str) -> str | None:
    """Ask DuckDB what geometry types actually live in the result table.
    Returns 'Polygon' / 'LineString' / 'Point' / 'MultiPolygon' / 'GeometryCollection'
    etc., matching ST_GeometryType output. Returns None on no rows / errors."""
    try:
        rows = session.conn.execute(
            f"SELECT DISTINCT ST_GeometryType({_quote_ident(geom_col)}) "
            f"FROM {_quote_ident(table)} WHERE {_quote_ident(geom_col)} IS NOT NULL LIMIT 5"
        ).fetchall()
    except Exception:
        return None
    types = [r[0] for r in rows if r[0]]
    if not types:
        return None
    if len(types) == 1:
        return types[0]
    # Mixed types — report the mix so metadata doesn't silently lie.
    return "|".join(sorted(types))


def _register_result(
    session: Session, new_name: str, parents: list[str], created_by: str,
) -> LayerMeta:
    schema = _column_schema(session.conn, new_name)
    geom_col = _detect_geometry_column(schema)
    bbox = _bbox_from_table(session.conn, new_name, geom_col) if geom_col else None
    n = session.conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(new_name)}").fetchone()[0]
    parent_metas = [session.layers[p] for p in parents if p in session.layers]

    # Re-probe geometry type from the actual materialized rows, not the parent.
    # Inheriting from a_layer is wrong when the operation changes geometry kind
    # (polygon × point intersection → point, dissolve → MultiPolygon, etc.).
    geom_type = None
    if geom_col:
        geom_type = _probe_geom_type(session, new_name, geom_col)
    if geom_type is None and parent_metas:
        geom_type = parent_metas[0].geometry_type

    meta = LayerMeta(
        name=new_name, feature_count=int(n),
        geometry_type=geom_type,
        bbox=bbox, attributes=schema,
        created_by=created_by, created_at=datetime.utcnow(),
        provenance=_merge_provenance(*parent_metas),
        parent_layers=parents,
    )
    meta.attributes["__geom_col__"] = geom_col or ""
    session.register(meta)
    return meta


def _require_layer(session: Session, name: str) -> LayerMeta:
    m = session.layers.get(name)
    if m is None:
        raise OpError(f"unknown layer '{name}'. Available: {list(session.layers)}")
    return m


def _geom_col(meta: LayerMeta) -> str:
    g = meta.attributes.get("__geom_col__") or ""
    if not g:
        raise OpError(f"layer '{meta.name}' has no geometry")
    return g


NUMERIC_SQL_TYPE_PREFIXES = (
    "INTEGER", "BIGINT", "DOUBLE", "FLOAT", "REAL", "DECIMAL",
    "HUGEINT", "UHUGEINT", "TINYINT", "SMALLINT",
    "UINTEGER", "UBIGINT", "USMALLINT", "UTINYINT",
)


def is_numeric_sql_type(t: str | None) -> bool:
    """Treat DuckDB type strings as numeric if their upper-cased form starts
    with any of `NUMERIC_SQL_TYPE_PREFIXES`. Used by quick_stats + stats."""
    if not t:
        return False
    u = t.upper()
    return any(u.startswith(p) for p in NUMERIC_SQL_TYPE_PREFIXES)


def _assert_predicate(s: str, arg: str) -> None:
    """Parse `s` as a SQL predicate/scalar expression. Rejects multi-statement
    input, which is how we prevent SQL injection via the WHERE / expression /
    ORDER BY args. Literal `;` inside quoted strings passes — only a real
    statement separator fails. Shared by filter/update_field/add_field/
    top_n/baseline_stats/classify/batch_iterate's where arg."""
    if not s:
        return
    try:
        stmts = sqlglot.parse(f"SELECT 1 FROM _t WHERE ({s})", read="duckdb")
    except sqlglot.errors.ParseError as e:
        raise OpError(f"`{arg}` is not a valid SQL expression: {e}")
    stmts = [x for x in stmts if x is not None]
    if len(stmts) != 1:
        raise OpError(
            f"`{arg}` must be a single SQL expression "
            f"(got {len(stmts)} statements after parsing)"
        )


_no_semicolon = _assert_predicate  # back-compat alias for existing callers
_assert_single_sql_expression = _assert_predicate  # ditto


def _fmt_cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.3f}" if abs(v) < 1e6 else f"{v:.3e}"
    s = str(v).replace("\n", " ").replace("|", "\\|")
    return s if len(s) < 80 else s[:77] + "..."


def _sql_literal(v) -> str:
    """Render a Python scalar as a SQL literal. Used by classify()."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    # String — single-quote-escape.
    s = str(v).replace("'", "''")
    return f"'{s}'"
