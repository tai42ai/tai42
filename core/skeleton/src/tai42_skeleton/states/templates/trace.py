"""The ``_trace`` property injection.

Under a tracing attachment the effective schema must admit the ``_trace`` field the
platform ``apply`` chokepoint stamps on every write — even where
``additionalProperties: false`` would otherwise forbid it. These transforms add ``_trace``
to every object schema within a fragment.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

# The five trace fields the effective schema admits on every object under a tracing
# attachment; the platform ``apply`` chokepoint stamps them. ``at`` is always present;
# ``meta``/``run``/``turn``/``inbound`` are null when the writer has none (a hook,
# schedule, api, or a builtin ``state_*`` tool supplies no meta, run, turn, or inbound).
# ``meta`` is the consumer's opaque provenance bag, stored and echoed as an object.
_TRACE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "meta": {"type": ["object", "null"]},
        "run": {"type": ["string", "null"]},
        "turn": {"type": ["string", "null"]},
        "inbound": {"type": ["string", "null"]},
        "at": {"type": "string"},
    },
}

# JSON-Schema keyword groups that hold child subschemas, by how the value nests them:
# a dict whose VALUES are subschemas; a value that is a subschema OR a list of them; a
# value that is a list of subschemas.
_DICT_OF_SUBSCHEMAS = ("properties", "patternProperties", "$defs", "definitions")
_SCHEMA_OR_LIST = ("items", "additionalProperties", "contains", "propertyNames")
_LIST_OF_SUBSCHEMAS = ("prefixItems", "allOf", "anyOf", "oneOf")


def _inject_trace(schema: dict[str, Any]) -> dict[str, Any]:
    """A deep copy of ``schema`` with a ``_trace`` property added to EVERY object schema within it.

    Covers nested objects, array items, ``additionalProperties`` schemas and combinators, so a
    document validated whole under a tracing attachment admits the stamped field even where
    ``additionalProperties: false`` would otherwise forbid it.
    """
    node = copy.deepcopy(schema)
    _inject_trace_inplace(node)
    return node


def _child_subschemas(node: dict[str, Any]) -> Iterator[Any]:
    """Every child subschema of an object-schema ``node`` across the JSON-Schema keyword groups.

    The nodes ``_trace`` injection must descend into.
    """
    for key in _DICT_OF_SUBSCHEMAS:
        sub = node.get(key)
        if isinstance(sub, dict):
            yield from sub.values()
    for key in _SCHEMA_OR_LIST:
        sub = node.get(key)
        if isinstance(sub, dict):
            yield sub
        elif isinstance(sub, list):
            yield from sub
    for key in _LIST_OF_SUBSCHEMAS:
        sub = node.get(key)
        if isinstance(sub, list):
            yield from sub


def _inject_trace_inplace(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _inject_trace_inplace(item)
        return
    if not isinstance(node, dict):
        return
    for child in _child_subschemas(node):
        _inject_trace_inplace(child)
    if node.get("type") == "object":
        props = node.setdefault("properties", {})
        if isinstance(props, dict):
            props["_trace"] = copy.deepcopy(_TRACE_SCHEMA)
