"""Opaque-transport bounds for a participant's structured submission.

``validate_bounded_object`` bounds an arbitrary caller-supplied JSON object as pure
transport (shape, string keys, nesting depth, finite numbers, serialized size);
``validate_inbound_form`` is the ask-less-form wrapper over it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

# Inbound-form transport bounds. The platform carries a participant's structured submission (an
# ask-less form's answers) to the turn as opaque, untrusted data; these bounds cap the
# transport alone (JSON-object shape, string keys, finite numbers, nesting depth, total
# serialized size) — never the submission's meaning or its conformance to any schema.
INBOUND_FORM_MAX_BYTES = 32 * 1024
INBOUND_FORM_MAX_DEPTH = 64


def _check_object_shape_and_depth(value: object, what: str) -> None:
    # The value is a JSON object (a dict — never a list or a scalar); every object key is a
    # string (a non-string key would be silently coerced by serialization, altering the object);
    # nesting stays within ``INBOUND_FORM_MAX_DEPTH`` container levels — checked ITERATIVELY, so an
    # arbitrarily deep (or self-referential) payload is a clean refusal, never a ``RecursionError``.
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object")
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > INBOUND_FORM_MAX_DEPTH:
            raise ValueError(f"{what} nests deeper than the {INBOUND_FORM_MAX_DEPTH} container levels allowed")
        if isinstance(node, dict):
            children = cast("Mapping[object, object]", node)
            for key in children:
                if not isinstance(key, str):
                    raise ValueError(f"{what} object keys must be strings")
            stack.extend((child, depth + 1) for child in children.values())
        elif isinstance(node, (list, tuple)):
            stack.extend((child, depth + 1) for child in cast("Sequence[object]", node))


def _check_json_serializable_size(value: object, what: str) -> None:
    # Every number is finite (``allow_nan=False`` — ``NaN``/``Infinity`` are not JSON) and every
    # value JSON-serializable; the serialized form fits in ``INBOUND_FORM_MAX_BYTES`` UTF-8 bytes.
    try:
        serialized = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except ValueError as exc:
        raise ValueError(f"{what} numbers must be finite (NaN and Infinity are not JSON)") from exc
    except TypeError as exc:
        raise ValueError(f"{what} must contain only JSON-serializable values") from exc
    total_bytes = len(serialized.encode())
    if total_bytes > INBOUND_FORM_MAX_BYTES:
        raise ValueError(f"{what} serializes to {total_bytes} bytes, over the {INBOUND_FORM_MAX_BYTES} allowed")


def validate_bounded_object(value: object, *, what: str) -> dict[str, Any]:
    """Refuse (``ValueError`` naming the first violated bound) or return the dict unchanged.

    The shared bounded-transport check for an opaque caller-supplied JSON object; ``what``
    names the object in every message. Checks, in order: the value is a JSON object (a dict —
    never a list or a scalar); every object key is a string (a non-string key would be
    silently coerced by serialization, altering the object); nesting stays within
    ``INBOUND_FORM_MAX_DEPTH`` container levels — checked ITERATIVELY, so an arbitrarily deep
    (or self-referential) payload is a clean refusal, never a ``RecursionError`` from the
    interpreter stack; every number is finite (``allow_nan=False`` — ``NaN``/``Infinity`` are
    not JSON) and every value JSON-serializable; the serialized form fits in
    ``INBOUND_FORM_MAX_BYTES`` UTF-8 bytes. Messages name the violated bound and NEVER a
    submitted value — the contents are opaque data and must never surface in a log or an error.
    """
    _check_object_shape_and_depth(value, what)
    _check_json_serializable_size(value, what)
    # Honest by construction: the walk above verified the value is a dict with string keys.
    return cast("dict[str, Any]", value)


def validate_inbound_form(form: object) -> dict[str, Any]:
    """Refuse (``ValueError``) or return the participant submission dict unchanged — the ask-less
    form's answers bounded as pure transport by :func:`validate_bounded_object` (``what="form"``);
    the contents stay opaque, untrusted participant data, never schema-conformant."""
    return validate_bounded_object(form, what="form")
