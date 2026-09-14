"""Regime path matching and static path validation.

A template's per-path writer regimes match a record path to the governing rule, and a
regime path is checked statically against the fragment structure at validation.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.states.errors import TemplateValidationError

from tai42_skeleton.states.templates.model import StateTemplate
from tai42_skeleton.states.templates.parameters import _is_marker

REGIMES = frozenset({"single", "composing", "free"})


def _pattern_prefix_matches(pattern: list[str], path: list[Any]) -> bool:
    """Whether ``pattern`` (with ``"*"`` wildcards) matches a leading run of ``path`` —
    the regime is declared AT or ABOVE the fill; ``"*"`` matches one index or key."""
    if len(pattern) > len(path):
        return False
    return all(seg == "*" or seg == path[i] for i, seg in enumerate(pattern))


def regime_for(template: StateTemplate, relative_path: list[Any]) -> str:
    """The regime governing ``relative_path`` — the ``regime`` of the LONGEST (most
    specific) declared regime path that matches it as a prefix, else ``"free"``."""
    best = "free"
    best_len = -1
    for rule in template.regimes:
        if _pattern_prefix_matches(rule.path, relative_path) and len(rule.path) > best_len:
            best = rule.regime
            best_len = len(rule.path)
    return best


def path_overlaps(a: list[Any], b: list[Any]) -> bool:
    """Whether two paths overlap — equal, one a prefix/descendant of the other —
    comparing ``"*"`` in either as a match for one segment on the other side."""
    n = min(len(a), len(b))
    return all(a[i] == "*" or b[i] == "*" or a[i] == b[i] for i in range(n))


def _descend_wildcard(node: dict[str, Any], path: list[str]) -> Any:
    """The ``"*"`` step: descend through ``items`` / ``additionalProperties`` / an open
    object, returning the next node or raising."""
    items = node.get("items")
    addl = node.get("additionalProperties")
    if isinstance(items, dict):
        return items
    if isinstance(addl, dict):
        return addl
    if addl is True or isinstance(node.get("patternProperties"), dict):
        return {}
    raise TemplateValidationError(
        f"regime path {path} uses '*' where the fragment has no items or additionalProperties"
    )


def _descend_key(node: dict[str, Any], seg: str, path: list[str]) -> Any:
    """The literal-key step: descend through a declared property / ``additionalProperties``
    / an open object, returning the next node or raising."""
    props = node.get("properties")
    addl = node.get("additionalProperties")
    if isinstance(props, dict) and seg in props:
        return props[seg]
    if isinstance(addl, dict):
        return addl
    if addl is True:
        return {}
    raise TemplateValidationError(f"regime path segment {seg!r} in {path} is not a property of the fragment")


def _validate_regime_path(fragment: dict[str, Any], path: list[str]) -> None:
    """Walk a regime ``path`` statically over the (defaults-substituted) fragment: a
    literal key must be a declared property (or admitted by an open object), and ``"*"``
    is allowed ONLY where the schema has ``items`` or ``additionalProperties``. A
    no-default parameter marker is opaque — traversal into it accepts the remaining
    segments."""
    node: Any = fragment
    for seg in path:
        if _is_marker(node):
            return
        if not isinstance(node, dict):
            raise TemplateValidationError(f"regime path {path} descends past the fragment's structure at {seg!r}")
        node = _descend_wildcard(node, path) if seg == "*" else _descend_key(node, seg, path)
