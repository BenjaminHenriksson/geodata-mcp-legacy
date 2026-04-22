"""execute_sql — validated read-only SELECT sandbox."""
from __future__ import annotations

import threading
from datetime import datetime

import sqlglot
from sqlglot import expressions as sqlexp

from ..loader import LoadError as OpError, _quote_ident
from ..session import Operation, Session
from ._util import _fmt_cell, _register_result


_DISALLOWED = (
    sqlexp.Insert, sqlexp.Update, sqlexp.Delete, sqlexp.Create,
    sqlexp.Drop, sqlexp.Alter, sqlexp.Merge, sqlexp.Copy,
)

# DuckDB table/scalar functions that touch the filesystem, network, or
# extension loader. These are rejected inside execute_sql even though their
# statements (plain SELECT) would otherwise pass validation. Catalog data is
# reachable via session layers — there is no legitimate user-facing need for
# these in execute_sql.
_DISALLOWED_FUNCTIONS = {
    # File readers
    "read_csv", "read_csv_auto", "read_parquet", "parquet_scan",
    "read_json", "read_json_auto", "read_json_objects", "json_scan",
    "read_blob", "read_text",
    "read_xlsx", "read_excel",
    # File system enumeration
    "glob", "parquet_schema", "parquet_metadata",
    "csv_sniffer", "sniff_csv",
    # Extension + attach (mostly already blocked by statement-type check,
    # but belt-and-braces)
    "load_extension", "install_extension",
    "attach", "detach",
    # httpfs / S3 direct
    "httpfs_register_secret", "s3_register_secret",
    # Process / environment
    "current_schemas", "pg_listen", "pg_terminate_backend",
}

# URL schemes we never want to see in a string literal. http(s) is the main
# SSRF concern; file:// + s3:// + gs:// etc. are exfiltration/privilege paths.
_DISALLOWED_URL_PREFIXES = (
    "http://", "https://", "ftp://", "ftps://",
    "file://", "s3://", "gs://", "r2://",
    "azure://", "abfs://", "abfss://",
    "hf://", "gcs://", "oss://",
)

# Absolute paths outside the catalog data dir are rejected inside execute_sql
# strings. This catches read_text('/etc/hostname') even if the function name
# was obfuscated. The catalog data directory itself is never accessed
# directly from execute_sql — it's always via session layers — so blocking
# all absolute paths here is safe.
_ABS_PATH_PREFIXES = ("/etc", "/home", "/root", "/var", "/proc", "/sys",
                      "/boot", "/usr", "/opt", "/srv", "/run", "/tmp")

# Per-query caps to stop `repeat('a', 1e10)` / `generate_series(0, 1e10)`
# from chewing memory even within the 256 MB session cap.
_MAX_LITERAL_LARGE_INT = 10_000_000


class SqlError(OpError):
    pass


def _func_name(node: sqlexp.Expression) -> str | None:
    """Try hard to get the callable name out of a sqlglot expression node."""
    if isinstance(node, sqlexp.Anonymous):
        return (node.name or "").lower()
    if isinstance(node, sqlexp.Func):
        # Some Func subclasses (Sum, Count, ...) have a class-level name.
        name = getattr(type(node), "sql_name", None)
        if callable(name):
            try:
                return name().lower()
            except TypeError:
                pass
        return type(node).__name__.lower()
    return None


def _validate_sql(sql: str) -> sqlexp.Expression:
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.ParseError as e:
        raise SqlError(f"parse error: {e}")
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SqlError(f"expected exactly 1 statement, got {len(statements)}")
    stmt = statements[0]
    if not isinstance(stmt, (sqlexp.Select, sqlexp.Subquery, sqlexp.With, sqlexp.Union)):
        raise SqlError(f"only SELECT/WITH/UNION allowed; got {stmt.key}")

    for node in stmt.walk():
        # DDL / DML anywhere in the tree → reject
        if isinstance(node, _DISALLOWED):
            raise SqlError(f"disallowed expression: {type(node).__name__}")

        # Dangerous function calls by name
        name = _func_name(node)
        if name and name in _DISALLOWED_FUNCTIONS:
            raise SqlError(
                f"disallowed function: {name}. "
                "Catalog data is reachable via session layers (see search_data / load) — "
                "read_*/glob/scan/attach/load_extension are blocked in execute_sql."
            )

        # String literals that are URL or absolute-path references
        if isinstance(node, sqlexp.Literal) and node.is_string:
            raw = str(node.this)
            lower = raw.lower()
            if any(lower.startswith(p) for p in _DISALLOWED_URL_PREFIXES):
                raise SqlError(
                    f"disallowed URL literal (scheme blocked): {raw[:80]!r}"
                )
            if any(lower.startswith(p) for p in _ABS_PATH_PREFIXES):
                raise SqlError(
                    f"disallowed absolute-path literal in SQL: {raw[:80]!r}. "
                    "Access files via catalog layers, not raw paths."
                )

        # Cap generators / repeat-like constructs that can OOM the session.
        # `repeat('a', 1e10)` and `generate_series(0, 1e10)` get caught here
        # by literal inspection, before DuckDB allocates.
        if isinstance(node, sqlexp.Literal) and not node.is_string:
            try:
                n = int(float(str(node.this)))
            except Exception:
                n = 0
            if n > _MAX_LITERAL_LARGE_INT:
                raise SqlError(
                    f"numeric literal {n} exceeds cap {_MAX_LITERAL_LARGE_INT} — "
                    "prevents memory exhaustion via repeat/generate_series."
                )
    return stmt


EXECUTE_SQL_TIMEOUT_S = 30.0
EXECUTE_SQL_MARKDOWN_ROW_CAP = 50


_GEOM_COLUMN_NAMES = {"geom", "geometry", "sp_geometry", "shape", "the_geom"}


def _describe_result_schema(conn, sql: str) -> list[tuple[str, str]]:
    """Ask DuckDB for the result schema of the user's SELECT without
    materializing all rows. Returns [(col, type_str), ...]."""
    try:
        rows = conn.execute(f"DESCRIBE ({sql})").fetchall()
    except Exception:
        return []
    return [(r[0], r[1]) for r in rows]


def execute_sql(
    session: Session, sql: str,
    *, description: str = "", result_name: str | None = None,
    geometry_column: str | None = None,
) -> dict:
    """Run a validated read-only SELECT. If geometry is produced, materialize
    as a new layer; otherwise return up to 50 rows as markdown.

    Validation errors raise SqlError (caller labels 'sql_rejected').
    Execution errors raise OpError (caller labels 'sql_failed').
    """
    _validate_sql(sql)  # → SqlError on failure

    # Detect geometry by result schema. `DESCRIBE (<sql>)` in DuckDB returns
    # types like 'GEOMETRY' or 'GEOMETRY(Point)'; only BLOB columns sneak in
    # when the engine drops the tag during cross-table expressions.
    schema = _describe_result_schema(session.conn, sql)
    auto_geom_col = next(
        (c for c, t in schema if t.upper().startswith("GEOMETRY")),
        None,
    )
    blob_geom_candidate = next(
        (c for c, t in schema
         if t.upper() == "BLOB" and c.lower() in _GEOM_COLUMN_NAMES),
        None,
    )

    def _run_with_timeout(sql_to_run: str, fetch: bool):
        timer = threading.Timer(EXECUTE_SQL_TIMEOUT_S, session.conn.interrupt)
        timer.start()
        try:
            cur = session.conn.execute(sql_to_run)
            if fetch:
                result = cur.fetchmany(EXECUTE_SQL_MARKDOWN_ROW_CAP)
                cols = [d[0] for d in session.conn.description]
                return result, cols
            return None, None
        finally:
            timer.cancel()

    # Decide layer-vs-table. Precedence:
    #   1. explicit geometry_column (force layer mode, cast to GEOMETRY)
    #   2. auto-detected column with GEOMETRY type
    #   3. table mode (with a hint if a BLOB column looks like it should've been geometry)
    chosen_geom_col = geometry_column or auto_geom_col
    if chosen_geom_col:
        # If it's the explicit hint and a column by that name exists, cast it
        # to GEOMETRY (covers the BLOB-leaks case).
        exists = any(c == chosen_geom_col for c, _ in schema)
        if not exists:
            raise OpError(
                f"geometry_column '{chosen_geom_col}' not in result schema "
                f"({[c for c, _ in schema]})"
            )
        if geometry_column is not None and auto_geom_col != geometry_column:
            # Wrap so the stored column is of type GEOMETRY.
            other_cols = [c for c, _ in schema if c != chosen_geom_col]
            other_select = (", ".join(_quote_ident(c) for c in other_cols) + ", ") if other_cols else ""
            wrapped = (
                f"SELECT {other_select}"
                f"{_quote_ident(chosen_geom_col)}::GEOMETRY AS {_quote_ident(chosen_geom_col)} "
                f"FROM ({sql}) _s"
            )
        else:
            wrapped = sql

        new_name = session.unique_layer_name(result_name or "sql_result")
        cta = f"CREATE TABLE {_quote_ident(new_name)} AS {wrapped}"
        try:
            _run_with_timeout(cta, fetch=False)
        except Exception as e:
            raise OpError(f"{type(e).__name__}: {e}")
        meta = _register_result(session, new_name, [], created_by="execute_sql")
        session.log(Operation(
            tool="execute_sql",
            args={"sql": sql, "description": description,
                  "geometry_column": geometry_column},
            result_layer=new_name,
            summary=f"{meta.feature_count} features",
            at=datetime.utcnow(),
        ))
        return {
            "mode": "layer",
            "layer_name": meta.name,
            "feature_count": meta.feature_count,
            "geometry_type": meta.geometry_type,
            "description": description,
        }

    # Table mode
    try:
        result, cols = _run_with_timeout(sql, fetch=True)
    except Exception as e:
        raise OpError(f"{type(e).__name__}: {e}")
    md = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for r in result:
        md.append("| " + " | ".join(_fmt_cell(v) for v in r) + " |")
    session.log(Operation(
        tool="execute_sql",
        args={"sql": sql, "description": description},
        result_layer=None, summary=f"{len(result)} rows",
        at=datetime.utcnow(),
    ))
    payload = {
        "mode": "table",
        "rows": len(result),
        "capped_at": EXECUTE_SQL_MARKDOWN_ROW_CAP,
        "description": description,
        "table_md": "\n".join(md),
    }
    if len(result) >= EXECUTE_SQL_MARKDOWN_ROW_CAP:
        payload["truncated"] = True
        payload["warning"] = (
            f"result capped at {EXECUTE_SQL_MARKDOWN_ROW_CAP} rows — the full "
            "result set may be larger. To get all rows, either narrow the "
            "query, or re-run with `result_name='...'` to materialize as a "
            "full layer (then iterate via `batch_iterate` or export)."
        )
    if blob_geom_candidate:
        payload["geometry_hint"] = (
            f"Column '{blob_geom_candidate}' looks like geometry but was stored "
            f"as BLOB (common with cross-layer expressions). Re-run with "
            f"`geometry_column='{blob_geom_candidate}'` to force layer mode, "
            "or cast it explicitly in SQL, e.g. "
            f"`SELECT …, {blob_geom_candidate}::GEOMETRY AS geom FROM ({{your sql}})`."
        )
    return payload
