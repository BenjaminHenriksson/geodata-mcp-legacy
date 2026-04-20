"""Phase 2 tool implementations: filter, spatial, stats, execute_sql, sources.

Provenance is inherited from parent layers automatically. Each result layer
records its parents in `LayerMeta.parent_layers` and the union of parents'
source references (deduped by dataset id) in `LayerMeta.provenance`.

This module was originally a single file; it is now a package split into
topical submodules (_util, spatial, sql, query, layers, fields, export,
checkpoint, inspect). The full public surface is re-exported here so
existing callers (`from .operations import …`) keep working unchanged.
"""
from __future__ import annotations

# Re-export OpError from loader for code that does
# `from geodata_mcp.operations import OpError`.
from ..loader import LoadError as OpError

from ._util import (
    _EPSG_RE,
    _UESC_PAIR_RE,
    _UESC_RE,
    _assert_predicate,
    _assert_single_sql_expression,
    _decode_single,
    _fmt_cell,
    _geom_col,
    _join_surrogates,
    _merge_provenance,
    _no_semicolon,
    _probe_geom_type,
    _register_result,
    _require_layer,
    _sql_literal,
    NUMERIC_SQL_TYPE_PREFIXES,
    decode_escapes_deep,
    decode_unicode_escapes,
    is_numeric_sql_type,
)

from .spatial import (
    SPATIAL_OPS,
    _clip_geom_expr,
    spatial_buffer,
    spatial_centroid,
    spatial_clip,
    spatial_convex_hull,
    spatial_dissolve,
    spatial_intersect,
    spatial_select_by_location,
)

from .sql import (
    EXECUTE_SQL_MARKDOWN_ROW_CAP,
    EXECUTE_SQL_TIMEOUT_S,
    SqlError,
    _GEOM_COLUMN_NAMES,
    _describe_result_schema,
    _func_name,
    _validate_sql,
    execute_sql,
)

from .query import (
    BATCH_ITERATE_DEFAULT,
    BATCH_ITERATE_MAX,
    baseline_stats,
    batch_iterate,
    classify,
    filter_layer,
    frequencies,
    stats,
    top_n,
)

from .checkpoint import (
    _active_checkpoints_for,
    _reversible_for_layer,
    _snapshot_column,
    _snapshot_whole_layer,
    checkpoint,
    commit,
    rollback,
)

from .fields import (
    ANNOTATE_CAP,
    _normalize_field_type,
    _sanity_check_expr,
    add_field,
    annotate,
    drop_field,
    update_field,
)

from .layers import (
    CREATE_LAYER_CAP,
    create_layer,
    drop_layer,
    hide_layers,
    list_layers,
    rename_layer,
    set_notes,
)

from .export import (
    EXPORT_ROOT,
    EXPORT_TTL_S,
    VALID_EXPORT_FORMATS,
    _export_safe_select_list,
    _purge_expired_exports,
    _write_single_layer,
    export_and_cite,
    export_layer,
    export_layers,
)

from .inspect import (
    _ancestry,
    _inspect_one_point,
    inspect_location,
    inspect_locations,
    reverse_geocode,
    sources,
)


__all__ = [
    # Exceptions
    "OpError", "SqlError",

    # Constants
    "EXPORT_ROOT", "EXPORT_TTL_S", "VALID_EXPORT_FORMATS",
    "EXECUTE_SQL_TIMEOUT_S", "EXECUTE_SQL_MARKDOWN_ROW_CAP",
    "BATCH_ITERATE_MAX", "BATCH_ITERATE_DEFAULT",
    "ANNOTATE_CAP", "CREATE_LAYER_CAP",
    "NUMERIC_SQL_TYPE_PREFIXES",
    "SPATIAL_OPS",

    # Public ops — read / derive
    "filter_layer", "stats", "frequencies", "baseline_stats",
    "top_n", "classify", "batch_iterate",

    # Public ops — spatial
    "spatial_buffer", "spatial_centroid", "spatial_clip",
    "spatial_convex_hull", "spatial_dissolve", "spatial_intersect",
    "spatial_select_by_location",

    # Public ops — SQL sandbox
    "execute_sql",

    # Public ops — field-level mutations
    "add_field", "update_field", "drop_field", "annotate",

    # Public ops — layer-level
    "create_layer", "drop_layer", "rename_layer",
    "list_layers", "set_notes", "hide_layers",

    # Public ops — export
    "export_layer", "export_layers", "export_and_cite",

    # Public ops — checkpoint/undo
    "checkpoint", "rollback", "commit",

    # Public ops — inspection / provenance
    "sources", "inspect_location", "inspect_locations", "reverse_geocode",

    # Unicode helpers (imported by session/loader)
    "decode_unicode_escapes", "decode_escapes_deep",

    # Internal helpers that external callers happen to reach into
    # (server.py / session.py / loader.py). Keep them public until they
    # can be cleaned up.
    "_assert_predicate", "_assert_single_sql_expression", "_no_semicolon",
    "_reversible_for_layer", "_active_checkpoints_for",
    "_snapshot_column", "_snapshot_whole_layer",
    "is_numeric_sql_type", "_fmt_cell", "_sql_literal",
    "_require_layer", "_register_result", "_merge_provenance",
    "_probe_geom_type", "_geom_col",
    "_validate_sql", "_describe_result_schema", "_func_name",
    "_export_safe_select_list", "_purge_expired_exports",
    "_write_single_layer",
    "_clip_geom_expr",
    "_ancestry", "_inspect_one_point",
    "_normalize_field_type", "_sanity_check_expr",
    "_EPSG_RE", "_UESC_PAIR_RE", "_UESC_RE",
    "_decode_single", "_join_surrogates",
    "_GEOM_COLUMN_NAMES",
]
