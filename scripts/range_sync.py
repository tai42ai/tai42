#!/usr/bin/env python3
"""Derive first-party ``tai42-*`` version ranges from the released member versions
and rewrite them in place — runnable entrypoint + public facade.

The implementation lives in the private ``_range_sync`` package; this module
re-exports its public surface (so ``import range_sync`` keeps resolving every
symbol) and is the entrypoint the CI lanes invoke by path. See
``_range_sync.cli`` for the full derivation rules and CLI.
"""

from __future__ import annotations

from _range_sync.cli import _print_descriptor_untouched, _print_preserved, _repo_root, main
from _range_sync.contract_pins import (
    _CONTRACT_RE,
    CONTRACT_PACKAGE,
    _contract_range,
    _descriptor_range_from_spec,
    _preserved_contract_ranges,
    contract_yaml_value,
    descriptor_only_contract_files,
    plugin_descriptor_files,
    rewrite_contract_yaml,
    scaffold_descriptor_files,
    shipped_descriptor_files,
)
from _range_sync.members import (
    DEFAULT_WORKSPACE_GLOBS,
    FIRST_PARTY_PREFIX,
    _load_toml,
    _normalize_name,
    _requirement_strings,
    discover_members,
    first_party_versions,
    workspace_globs,
)
from _range_sync.pyproject_specs import (
    PIN_KEY,
    PIN_TABLE,
    Preserved,
    PyprojectAnalysis,
    SpecChange,
    _first_party_dep_names,
    _replace_quoted,
    analyze_pyproject,
    compute_pyproject_changes,
    pinned_deps,
    rewrite_pyproject_text,
)
from _range_sync.sync import (
    SyncReport,
    _apply_descriptor_writes,
    _apply_pyproject_writes,
    _assert_no_stray_pin_tables,
    _check_descriptors,
    _collect_pyproject_analysis,
    _empty_report,
    _format_drift,
    _resolve_descriptor_target,
    _self_assert,
    apply,
    check,
)
from _range_sync.version_ranges import (
    _CAP_RE,
    _COMPAT_RE,
    _FLOOR_RE,
    _FLOOR_VERSION_RE,
    _REQ_RE,
    ParsedRequirement,
    _major_structure,
    _pin_guarded,
    derive_range,
    is_cross_major,
    parse_requirement,
)

__all__ = [
    "CONTRACT_PACKAGE",
    "DEFAULT_WORKSPACE_GLOBS",
    "FIRST_PARTY_PREFIX",
    "PIN_KEY",
    "PIN_TABLE",
    "_CAP_RE",
    "_COMPAT_RE",
    "_CONTRACT_RE",
    "_FLOOR_RE",
    "_FLOOR_VERSION_RE",
    "_REQ_RE",
    "ParsedRequirement",
    "Preserved",
    "PyprojectAnalysis",
    "SpecChange",
    "SyncReport",
    "_apply_descriptor_writes",
    "_apply_pyproject_writes",
    "_assert_no_stray_pin_tables",
    "_check_descriptors",
    "_collect_pyproject_analysis",
    "_contract_range",
    "_descriptor_range_from_spec",
    "_empty_report",
    "_first_party_dep_names",
    "_format_drift",
    "_load_toml",
    "_major_structure",
    "_normalize_name",
    "_pin_guarded",
    "_preserved_contract_ranges",
    "_print_descriptor_untouched",
    "_print_preserved",
    "_replace_quoted",
    "_repo_root",
    "_requirement_strings",
    "_resolve_descriptor_target",
    "_self_assert",
    "analyze_pyproject",
    "apply",
    "check",
    "compute_pyproject_changes",
    "contract_yaml_value",
    "derive_range",
    "descriptor_only_contract_files",
    "discover_members",
    "first_party_versions",
    "is_cross_major",
    "main",
    "parse_requirement",
    "pinned_deps",
    "plugin_descriptor_files",
    "rewrite_contract_yaml",
    "rewrite_pyproject_text",
    "scaffold_descriptor_files",
    "shipped_descriptor_files",
    "workspace_globs",
]


if __name__ == "__main__":
    raise SystemExit(main())
