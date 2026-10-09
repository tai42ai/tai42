"""Diagnostics for a tool run's outcome at a conversation turn.

A FAILURE is RAISED as the contract's :class:`~tai42_contract.interactions.RunTerminalFailed`,
carrying the driver's outcome as an OPAQUE payload; the turn catches it and records the payload
WHOLE through :func:`~tai42_skeleton.interactions.terminal_failure.failed_outcome_detail`, never
reading a key inside it. A RETURNED value is a success the turn maps through ``reply_expr`` with NO
status inspection, and a still-parked run is already a typed contract value the visit normalises.
When the reply mapping faults, the turn logs a value-free structural walk of the value: names, types,
lengths and sizes only — no participant content ever crosses into a log line.
"""

from __future__ import annotations

import json

#: The structural walk lists at most this many key names per level, so a wide value cannot bloat
#: the log line.
_RESULT_SHAPE_KEY_CAP = 40

#: The walk names keys this many levels deep; the values at the last level are reported by type and
#: approximate serialized size.
_RESULT_SHAPE_DEPTH = 2


def _key_names(mapping: dict[object, object]) -> list[str]:
    """The mapping's string keys, sorted and capped — NAMES only, never a value."""
    return sorted(key for key in mapping if isinstance(key, str))[:_RESULT_SHAPE_KEY_CAP]


def _leaf(value: object) -> str:
    """A last-level value as its type and approximate serialized size (``-1`` when unserializable)."""
    try:
        size = len(json.dumps(value, default=str))
    except Exception:
        size = -1
    return f"type={type(value).__name__} size={size}"


def _walk(value: object, level: int) -> str:
    if isinstance(value, dict):
        names = _key_names(value)
        children = [
            f"{name}: {_leaf(value[name]) if level == _RESULT_SHAPE_DEPTH else _walk(value[name], level + 1)}"
            for name in names
        ]
        return f"type=dict keys={names} {{{'; '.join(children)}}}" if children else f"type=dict keys={names}"
    if isinstance(value, (list, tuple)):
        first = f" first={type(value[0]).__name__}" if value else ""
        return f"type={type(value).__name__} len={len(value)}{first}"
    length = len(value) if isinstance(value, (str, bytes, set)) else None
    return f"type={type(value).__name__}" + (f" len={length}" if length is not None else "")


def _result_shape(result: object) -> str:
    """A VALUE-FREE structural walk of a tool result, logged when the reply mapping faults.

    For a dict: its sorted key names (capped per level), recursing one level further, where each
    value is its type and approximate serialized size; for a list: its length and first element's
    type; for a scalar: its type and its length when it has one. It reads no named key and renders
    no value — a status token is a value too.
    """
    return _walk(result, 1)
