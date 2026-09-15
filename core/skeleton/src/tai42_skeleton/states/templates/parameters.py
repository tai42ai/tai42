"""Parameter markers and fragment substitution.

A ``{"$parameter": "<name>"}`` marker in a template fragment is replaced with a supplied
value; the tools here recognize markers, name them, and substitute them purely.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from tai42_contract.states.errors import TemplateValidationError


def _is_marker(node: Any) -> bool:
    """Whether ``node`` is a ``{"$parameter": "<name>"}`` fill marker."""
    return isinstance(node, dict) and "$parameter" in node


def _marker_name(node: dict[str, Any]) -> str:
    """The parameter name of a marker, refusing a malformed marker loudly."""
    if len(node) != 1 or not isinstance(node["$parameter"], str) or not node["$parameter"]:
        raise TemplateValidationError(
            f"a $parameter marker must be exactly {{'$parameter': '<name>'}} with a non-empty name, got {node!r}"
        )
    return node["$parameter"]


def substitute_parameters(fragment: Any, values: Mapping[str, Any]) -> Any:
    """Replace every ``{"$parameter": "<name>"}`` marker named in ``values`` with a deep copy of that value.

    A marker whose name is absent is left intact (the validation path substitutes only DEFAULTS and leaves
    no-default markers standing). Pure — the input is never mutated.
    """
    if _is_marker(fragment):
        name = _marker_name(fragment)
        return copy.deepcopy(values[name]) if name in values else {"$parameter": name}
    if isinstance(fragment, dict):
        return {k: substitute_parameters(v, values) for k, v in fragment.items()}
    if isinstance(fragment, list):
        return [substitute_parameters(v, values) for v in fragment]
    return fragment


def _iter_marker_names(node: Any):
    """Yield every parameter name referenced by a marker anywhere in ``node``."""
    if _is_marker(node):
        yield _marker_name(node)
        return
    if isinstance(node, dict):
        for v in node.values():
            yield from _iter_marker_names(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_marker_names(v)
