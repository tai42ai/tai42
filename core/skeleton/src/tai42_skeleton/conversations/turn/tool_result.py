"""Diagnostics for a tool run's outcome at a conversation turn.

A FAILURE is RAISED as the contract's :class:`~tai42_contract.interactions.RunTerminalFailed`,
carrying the driver's outcome as an OPAQUE payload; the turn catches it and records the payload
WHOLE (:func:`_failed_outcome_detail`), never reading a key inside it. A RETURNED value is a
success the turn maps through ``reply_expr`` with NO status inspection, and a still-parked run is
already a typed contract value the visit normalises. This module no longer classifies a returned
envelope by status: it renders a raised failure's payload for the record, and a value-free
structural shape diagnostic for a reply-mapping fault. No participant content ever crosses into a
log line.
"""

from __future__ import annotations

import json
from typing import Any

#: A rendered value (a raised failure's whole payload, or a diagnostic value) is capped, so a
#: large payload cannot bloat the recorded detail.
_FAILED_RESULT_DETAIL_LIMIT = 200

#: Appended to a value the cap clipped, so a truncated detail never reads as a complete one.
_FAILED_RESULT_DETAIL_ELLIPSIS = "…(truncated)"


def _capped_repr(value: object) -> str:
    """``value``'s repr, clipped to :data:`_FAILED_RESULT_DETAIL_LIMIT` with an explicit truncation marker.

    Never silently: a clipped value that read as a whole one would make a recorded detail lie about
    the payload it came from.
    """
    text = repr(value)
    if len(text) <= _FAILED_RESULT_DETAIL_LIMIT:
        return text
    return text[:_FAILED_RESULT_DETAIL_LIMIT] + _FAILED_RESULT_DETAIL_ELLIPSIS


def _failed_outcome_detail(outcome: object) -> str:
    """The internal detail for a RAISED :class:`RunTerminalFailed`, carrying its payload WHOLE.

    The outcome is an OPAQUE payload the driver that failed wrote; the turn records it as-is
    (capped), never pulling ``error_kind`` / ``missing_results`` / ``session_id`` out by name. It
    is recorded and logged, never delivered — the delivered reply is the route's generic
    client-safe error. A UI shows the stored payload generically.
    """
    return f"tool run failed: {_capped_repr(outcome)}"


#: The failure-path structural diagnostic lists at most this many key names per level, so a
#: wide envelope cannot bloat the log line.
_RESULT_SHAPE_KEY_CAP = 40


def _shape_key_names(mapping: dict[Any, Any]) -> list[str]:
    """The mapping's string keys, sorted and capped — NAMES only.

    An envelope key, a ``result`` key, or a ``return_result`` surface id is a protocol/authoring
    identifier, never participant content, so its NAME is client-safe to log; its VALUE is not and
    never reaches here.
    """
    return sorted(key for key in mapping if isinstance(key, str))[:_RESULT_SHAPE_KEY_CAP]


def _shape_surface_sizes(outputs: dict[Any, Any]) -> dict[str, int]:
    """Each ``result.outputs`` surface's NAME mapped to the approximate serialized SIZE of its value.

    Never the value itself. The size is the one datum that separates an ABSENT surface (not here at
    all) from a PRESENT-BUT-EMPTY one (a size of ~2, an empty list/object), decided without a
    participant byte reaching the log. An unserializable value records ``-1`` rather than rendering
    it.
    """
    sizes: dict[str, int] = {}
    for name in _shape_key_names(outputs):
        try:
            sizes[name] = len(json.dumps(outputs[name], default=str))
        except Exception:
            sizes[name] = -1
    return sizes


def _result_shape(result: object) -> str:
    """A VALUE-FREE structural descriptor of a tool result, logged when the reply mapping faults.

    Yields the envelope's SHAPE as ground truth on the next live failure — which flagged surface
    is ABSENT vs PRESENT-BUT-EMPTY — instead of an inference from the guard's error text.

    Client-safe by construction: it emits only STRUCTURE — the type, the envelope's own key NAMES
    (protocol/authoring identifiers, never participant content), the ``status`` token, the
    ``return_result`` surface NAMES present under ``result.outputs`` with their approximate
    serialized SIZES, and the ``missing_results`` surface NAMES the run reported unproduced. A
    participant's message text lives in the VALUES under those surfaces, which this NEVER renders — only
    names, counts, and sizes cross into the log.
    """
    if not isinstance(result, dict):
        length = len(result) if isinstance(result, (str, bytes, list, tuple, dict, set)) else None
        return f"type={type(result).__name__}" + (f" len={length}" if length is not None else "")
    parts = [f"type=dict keys={_shape_key_names(result)}"]
    status = result.get("status")
    if isinstance(status, str):
        parts.append(f"status={status!r}")
    inner = result.get("result")
    if isinstance(inner, dict):
        parts.append(f"result_keys={_shape_key_names(inner)}")
        outputs = inner.get("outputs")
        if isinstance(outputs, dict):
            parts.append(f"outputs_surface_sizes={_shape_surface_sizes(outputs)}")
    missing = result.get("missing_results")
    if isinstance(missing, list):
        parts.append(
            f"missing_results={sorted(name for name in missing if isinstance(name, str))[:_RESULT_SHAPE_KEY_CAP]}"
        )
    return "; ".join(parts)
