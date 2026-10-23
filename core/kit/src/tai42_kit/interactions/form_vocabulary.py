"""The platform's form vocabulary: the ``visibleWhen`` evaluator, the drawing order and the shared form constants.

Every surface that validates, renders or evaluates a channel form reads these names, so a form draws,
orders and hides its fields the same way on every channel. The date-key names are defined once in the
contract, beside the build-time date rules, and listed here with the rest of the vocabulary.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

from tai42_contract.interactions.models.form_dates import DATE_CONSTRAINT_KEYS, FORM_WEEKDAYS, TIME_OF_KEY

__all__ = [
    "DATE_CONSTRAINT_KEYS",
    "FORMAT_PATTERNS",
    "FORM_WEEKDAYS",
    "RADIO_OPTION_THRESHOLD",
    "TIME_OF_KEY",
    "VISIBLE_WHEN_OPS",
    "drawing_order",
    "drawn_number_bounds",
    "evaluate_visible_when",
    "hidden_fields",
]

#: The ``visibleWhen`` operators; exactly one of them rides a predicate.
VISIBLE_WHEN_OPS: Final = ("equals", "in", "notEmpty")

#: The option-count threshold a renderer uses to choose a radio group over a dropdown/select
#: for a choice field: an ``enum`` (or per-send option list) with AT MOST this many choices
#: renders as a radio group, more as a dropdown. One shared, documented constant so every
#: surface draws the same short list the same way.
RADIO_OPTION_THRESHOLD: Final = 5

#: The value shape of each typed string ``format`` a form field may declare, one regex each. Every
#: pattern is anchored (``^``/``$``) and written in the syntax ECMAScript and Python regexes share
#: (``[0-9]`` rather than ``\d``), so a browser's ``new RegExp(pattern).test(value)``, a vendor
#: control's ``pattern`` and the server's ``re.fullmatch`` decide the same shape. ``email`` is one
#: ``@`` between a non-empty local part and a domain with a dot, no whitespace. The answer door
#: additionally checks that a ``date``/``time``/``date-time`` names a real calendar value.
FORMAT_PATTERNS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "date": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
        "time": r"^[0-9]{2}:[0-9]{2}(:[0-9]{2})?$",
        "date-time": (
            r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})$"
        ),
        "email": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    }
)


def _numeric(value: Any) -> int | float | None:
    # A JSON-Schema numeric bound; a bool is not one.
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def drawn_number_bounds(prop: Mapping[str, Any]) -> tuple[int | float | None, int | float | None]:
    """The inclusive ``(minimum, maximum)`` a surface draws natively on a number property's control.

    A bound that is absent or not a number reads ``None``. An ``integer`` property's bounds are the
    tightest whole numbers within them (``minimum: 1.5`` reads ``2``, ``maximum: 3.5`` reads ``3``,
    ``1.0`` reads ``1``): a control stepping in whole numbers from its minimum then admits exactly
    the whole numbers the schema admits. ``exclusiveMinimum``/``exclusiveMaximum`` are not drawn.
    """
    low = _numeric(prop.get("minimum"))
    high = _numeric(prop.get("maximum"))
    if prop.get("type") == "integer":
        low = math.ceil(low) if low is not None else None
        high = math.floor(high) if high is not None else None
    return low, high


def evaluate_visible_when(predicate: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    """Whether a property with ``predicate`` is VISIBLE given the submitted ``values``.

    ``equals`` is visible iff the controlling field equals the given value; ``in`` iff its
    value is one of the given list; ``notEmpty`` iff its value is present and not an empty
    string / list / object. An absent controlling value reads as unset (``None``).
    """
    controlling = values.get(predicate["field"])
    if "equals" in predicate:
        return controlling == predicate["equals"]
    if "in" in predicate:
        return controlling in predicate["in"]
    # notEmpty
    return controlling not in (None, "", [], {})


def hidden_fields(properties: Mapping[str, Any], values: Mapping[str, Any]) -> set[str]:
    """The ``properties`` HIDDEN by their ``visibleWhen`` predicate for the submitted ``values``.

    A property with no predicate is always visible. The predicate is evaluated on the
    submitted values once (no cascading re-evaluation): the documented meaning of a
    conditional field.
    """
    hidden: set[str] = set()
    for name, prop in properties.items():
        if not isinstance(prop, Mapping):
            continue
        predicate = prop.get("visibleWhen")
        if isinstance(predicate, Mapping) and not evaluate_visible_when(predicate, values):
            hidden.add(str(name))
    return hidden


def drawing_order(names: list[str], properties: Mapping[str, Any]) -> list[str]:
    """``names`` in drawing order: each ``timeOf`` time moved to sit right after the date it names.

    ``names`` is one page's fields, or every property of a page-less form or a readback, in their
    given order. A time moves only when the name it points at is in ``names`` and does not itself
    point at another listed name; any other time (its date not listed, a chain of times, a cycle)
    keeps its place. Every name appears exactly once and every other name keeps its relative
    order. Pure and idempotent, so every walker that orders the same list draws it the same way.
    """
    listed = set(names)
    target_of: dict[str, str] = {}
    for name in names:
        prop = properties.get(name)
        date_name = prop.get(TIME_OF_KEY) if isinstance(prop, Mapping) else None
        if isinstance(date_name, str) and date_name != name and date_name in listed:
            target_of[name] = date_name
    times_of: dict[str, list[str]] = {}
    for name in names:
        date_name = target_of.get(name)
        if date_name is not None and date_name not in target_of:
            times_of.setdefault(date_name, []).append(name)
    moved = {time_name for time_names in times_of.values() for time_name in time_names}
    ordered: list[str] = []
    for name in names:
        if name in moved:
            continue
        ordered.append(name)
        ordered.extend(times_of.get(name, ()))
    return ordered
