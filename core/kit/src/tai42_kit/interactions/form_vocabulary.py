"""The platform's form vocabulary: the ``visibleWhen`` operators and evaluator, and the shared form constants.

Every surface that validates, renders or evaluates a channel form reads these names, so a form draws
and hides its fields the same way on every channel.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "DATE_CONSTRAINT_KEYS",
    "FORM_WEEKDAYS",
    "RADIO_OPTION_THRESHOLD",
    "VISIBLE_WHEN_OPS",
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

#: The lowercase weekday names an ``unavailableDates`` entry may name to exclude a recurring
#: weekday (alongside explicit ``YYYY-MM-DD`` dates), indexed by ``date.weekday()``.
FORM_WEEKDAYS: Final = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


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
