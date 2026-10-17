"""The platform's form vocabulary: the ``visibleWhen`` operators and evaluator, and the shared form constants.

Every surface that validates, renders or evaluates a channel form reads these names, so a form draws
and hides its fields the same way on every channel.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "DATE_CONSTRAINT_KEYS",
    "FORMAT_PATTERNS",
    "FORM_WEEKDAYS",
    "RADIO_OPTION_THRESHOLD",
    "VISIBLE_WHEN_OPS",
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

#: The date-constraint keywords a ``date``/``date-time`` property may carry.
DATE_CONSTRAINT_KEYS: Final = ("minDate", "maxDate", "unavailableDates", "rangeStart", "minDays", "maxDays")

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

#: The lowercase weekday names an ``unavailableDates`` entry may name to exclude a recurring
#: weekday (alongside explicit ``YYYY-MM-DD`` dates), indexed by ``date.weekday()``.
FORM_WEEKDAYS: Final = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


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
