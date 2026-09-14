"""Platform int64 / msgpack integer range: the encode-range constants, the
oversized-integer finder, and the in-place tightening of integer schema nodes.

The checkpoint serializer encodes an ``int`` as a native msgpack integer, so a
value outside the encodable range aborts serialization; these helpers name such a
value and tighten every integer schema node to the platform range before a value
can be produced.
"""

import copy
from collections.abc import Iterator
from typing import Any

# Platform serialization ceiling: the checkpoint serializer encodes an ``int``
# as a native msgpack integer, which is bounded to signed 64-bit. An integer
# outside this range has no native encoding and aborts serialization, so every
# integer schema node is tightened to it before a value can be produced.
INT64_MIN = -9223372036854775808
INT64_MAX = 9223372036854775807

# Native msgpack integer encoding range: a signed 64-bit floor through an
# unsigned 64-bit ceiling. msgpack encodes any integer in this range; an integer
# outside it aborts the serializer. Wider than the int64 platform range above:
# an unsigned value in ``(INT64_MAX, MSGPACK_INT_MAX]`` encodes fine at the
# serializer even though it overflows a signed-int64 field.
MSGPACK_INT_MIN = -9223372036854775808
MSGPACK_INT_MAX = 18446744073709551615


def _overflow_children(obj: Any, path: str) -> list[tuple[str, Any]] | None:
    """The child ``(path, value)`` pairs of ``obj`` to recurse for a mapping,
    sequence, or attribute-bearing object; ``None`` for a leaf or opaque value."""
    if isinstance(obj, dict):
        return [(f"{path}[{key!r}]", value) for key, value in obj.items()]
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [(f"{path}[{index}]", value) for index, value in enumerate(obj)]
    if hasattr(obj, "__dict__"):
        return [(f"{path}.{name}", value) for name, value in vars(obj).items()]
    return None


def find_oversized_int(
    obj: Any,
    *,
    minimum: int,
    maximum: int,
    path: str = "",
    depth: int = 0,
    seen: set[int] | None = None,
) -> tuple[str, int] | None:
    """First ``(path, value)`` in ``obj`` whose integer falls outside
    ``[minimum, maximum]``, or ``None``.

    Walks mappings, sequences, and object/model attributes, guarding against
    cycles and unbounded depth. The bound is caller-supplied: the checkpoint
    guard scans the native msgpack range (``MSGPACK_INT_MIN``/``MSGPACK_INT_MAX``)
    to name the true encode-failure culprit, while structured-output validation
    scans the stricter int64 platform range (``INT64_MIN``/``INT64_MAX``)."""
    if depth > 200:
        return None
    # bool is an int subclass but always encodes; only true integers can overflow.
    if isinstance(obj, bool):
        return None
    if isinstance(obj, int):
        return (path or "<root>", obj) if obj < minimum or obj > maximum else None
    if isinstance(obj, (str, bytes, bytearray)):
        return None

    seen = seen if seen is not None else set()
    if id(obj) in seen:
        return None
    seen.add(id(obj))

    children = _overflow_children(obj, path)
    if children is None:
        return None
    for child_path, value in children:
        found = find_oversized_int(value, minimum=minimum, maximum=maximum, path=child_path, depth=depth + 1, seen=seen)
        if found is not None:
            return found
    return None


def inject_int64_bounds(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``schema`` with every integer node tightened to the
    platform int64 range.

    For each integer-typed node the bounds are tightened, never loosened:
    ``minimum = max(existing, INT64_MIN)`` and ``maximum = min(existing, INT64_MAX)``
    (an absent bound is set to the int64 edge). Walks every nested site an integer
    can hide in — ``properties``, ``patternProperties``, ``items``, ``prefixItems``,
    ``additionalProperties``, ``anyOf``/``oneOf``/``allOf`` and ``$defs`` (a ``$ref``
    is bounded through its ``$defs`` target). ``number`` (float) nodes are left
    untouched — only ``integer`` overflows the msgpack integer encoding.
    """
    result = copy.deepcopy(schema)
    _tighten_int64(result)
    return result


def _subschemas(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every child schema node of ``node`` — ``properties``/``patternProperties``/
    ``$defs`` values, ``anyOf``/``oneOf``/``allOf``/``prefixItems`` members,
    ``items``, and ``additionalProperties``."""
    for mapping_key in ("properties", "patternProperties", "$defs"):
        mapping = node.get(mapping_key)
        if isinstance(mapping, dict):
            yield from (sub for sub in mapping.values() if isinstance(sub, dict))

    for list_key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        subschemas = node.get(list_key)
        if isinstance(subschemas, list):
            yield from (sub for sub in subschemas if isinstance(sub, dict))

    items = node.get("items")
    if isinstance(items, dict):
        yield items
    elif isinstance(items, list):
        yield from (sub for sub in items if isinstance(sub, dict))

    additional = node.get("additionalProperties")
    if isinstance(additional, dict):
        yield additional


def _tighten_int64(node: Any) -> None:
    if not isinstance(node, dict):
        return

    node_type = node.get("type")
    if node_type == "integer" or (isinstance(node_type, list) and "integer" in node_type):
        node["minimum"] = max(node["minimum"], INT64_MIN) if "minimum" in node else INT64_MIN
        node["maximum"] = min(node["maximum"], INT64_MAX) if "maximum" in node else INT64_MAX

    for sub in _subschemas(node):
        _tighten_int64(sub)
