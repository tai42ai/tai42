"""The plain-JSON check a schema passes before any JSON Schema rule is read.

A schema an instance check can use is plain JSON: every number in it is finite, no value holds a
value it sits inside, and no value is an iterator. :func:`check_plain_json` walks every value at
every depth once, and each refusal names the top-level property the value sits under, or its
location in the schema.
"""

from __future__ import annotations

import cmath
import math
import numbers
from collections.abc import Collection, Iterator, Mapping
from decimal import Decimal
from typing import Any


def _owner_and_location(path: list[str], subject: str) -> tuple[str, list[str]]:
    # Who a fault at ``path`` belongs to: the top-level property it sits under when it has one
    # (with the rest of the path inside that property), else the schema itself.
    if path[:1] == ["properties"] and len(path) > 1:
        return f"{subject} property {path[1]!r}", path[2:]
    return subject, path


# A value's place in the schema: ``None`` for the schema itself, else its parent's place and
# the key under it. Linking to the parent keeps a walk linear in the schema's size however deep
# it nests; :func:`_steps` spells a place out only for a refusal.
_Place = tuple["_Place", str] | None


def _steps(place: _Place) -> list[str]:
    # The keys leading from the schema to ``place``.
    steps: list[str] = []
    while place is not None:
        place, key = place
        steps.append(key)
    steps.reverse()
    return steps


def check_plain_json(schema: Any, *, subject: str) -> None:
    """Raise ``ValueError`` when ``schema`` is not plain JSON an instance check can use; return ``None`` otherwise.

    Walks every value at every depth (bounds, ``enum``/``const`` members, defaults, examples,
    nested schemas) through every container a caller can pass (any mapping, stored as a JSON
    object, and any other collection but text and bytes, stored as a JSON array) and refuses a
    number that is not finite (an infinity or a NaN of any number type: ``float``, ``Decimal``,
    ``complex`` or any other real type), a value that holds one of the values it sits inside (a
    cycle, which only an in-process caller can build and no JSON text can carry), and an iterator
    (reading it to check it would use up what is stored). A value shared by two places that holds
    neither is plain JSON and is walked once. The message names the top-level property the value
    sits under and its location inside it, or its location in the schema.
    """
    # Depth-first with an exit marker per container, so ``inside`` holds exactly the containers
    # the current value sits inside; each value carries its place as a link to its parent's. A
    # container walked to its end is in ``done``: met again it holds no cycle and no fault.
    pending: list[tuple[bool, _Place, Any]] = [(False, None, schema)]
    inside: set[int] = set()
    done: set[int] = set()
    while pending:
        leaving, place, value = pending.pop()
        if leaving:
            inside.discard(id(value))
            done.add(id(value))
            continue
        if _not_finite(value):
            raise ValueError(f"{_value_at(_steps(place), subject)} that is not a finite number ({value})")
        if isinstance(value, Iterator):
            # A refusal of the schema the caller passed, raised as every schema refusal is.
            raise ValueError(  # noqa: TRY004
                f"{_value_at(_steps(place), subject)} that is an iterator ({type(value).__name__}); "
                "a schema must be plain JSON, and reading an iterator to check it uses it up"
            )
        if not _is_container(value) or id(value) in done:
            continue
        if id(value) in inside:
            raise ValueError(
                f"{_value_at(_steps(place), subject)} that loops back to a value it sits inside; "
                "a schema must be plain JSON, which has no cycles"
            )
        inside.add(id(value))
        pending.append((True, place, value))
        pending.extend((False, (place, str(key)), item) for key, item in reversed(_entries(value)))


def _not_finite(value: Any) -> bool:
    # An infinity or a NaN of any number type. Integers and fractions are always finite (and may
    # be too large to turn into a float to ask).
    if isinstance(value, Decimal):
        return not value.is_finite()
    if isinstance(value, numbers.Rational):
        return False
    if isinstance(value, numbers.Real):
        return not math.isfinite(value)
    if isinstance(value, numbers.Complex):
        return not cmath.isfinite(value)
    return False


def _value_at(path: list[str], subject: str) -> str:
    # The opening of a refusal for the value at ``path``.
    owner, location = _owner_and_location(path, subject)
    at = f" at {'/'.join(location)}" if location else ""
    return f"{owner} has a value{at}"


def _is_container(value: Any) -> bool:
    # A value holding others: a mapping, or any other collection but text and bytes.
    return isinstance(value, Collection) and not isinstance(value, str | bytes | bytearray)


def _entries(value: Any) -> list[tuple[Any, Any]]:
    # The ``(key, item)`` pairs a container holds (a mapping's by key, any other's by index);
    # none for a scalar.
    if isinstance(value, Mapping):
        return list(value.items())
    if _is_container(value):
        return list(enumerate(value))
    return []
