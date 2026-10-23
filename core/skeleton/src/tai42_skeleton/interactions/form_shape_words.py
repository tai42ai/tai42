"""The platform's plain words for a broken form shape — the one table every surface and the door share.

:func:`shape_sentence` turns a :class:`~tai42_contract.interactions.FormShapeKind` and the bound it
names into one plain English sentence. The answer door's refusal, the in-form checks of every
surface and the callback page's per-field messages all read it, so a broken shape reads the same
wherever the person meets it; a caller's ``wording.form.fields[<name>].invalid`` replaces it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, assert_never

from tai42_contract.interactions import FormShapeKind


def _number(value: Any) -> str:
    # A bound as a person reads it: ``1`` for 1 and 1.0, ``9.5`` for 9.5.
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _count(value: Any, one: str, many: str) -> str:
    return f"{_number(value)} {one if value == 1 else many}"


_TYPE_SENTENCES = {
    "integer": "Enter a whole number",
    "number": "Enter a number",
    "boolean": "Choose yes or no",
    "array": "Choose from the offered options",
    "string": "Enter text",
}

_FORMAT_SENTENCES = {
    "email": "Enter a valid email address",
    "date": "Enter a date as YYYY-MM-DD",
    "time": "Enter a time as HH:MM",
    "date-time": "Enter a date and time with its time zone",
}

_PATTERN_SENTENCE = "This value is not in the expected form"
_OTHER_SENTENCE = "Not an accepted value"


def _span_sentence(params: Mapping[str, Any]) -> str:
    low, high = params.get("minDays"), params.get("maxDays")
    if low is not None and high is not None:
        return f"Choose between {_number(low)} and {_number(high)} days"
    if low is not None:
        return f"Choose at least {_count(low, 'day', 'days')}"
    if high is not None:
        return f"Choose at most {_count(high, 'day', 'days')}"
    return "Choose a shorter or longer date range"


def shape_sentence(kind: FormShapeKind, params: Mapping[str, Any]) -> str:  # noqa: C901, PLR0912 one exhaustive match over the kinds is the platform's one table of words
    """The platform's plain sentence for a broken shape of ``kind``, naming the bound in ``params``.

    The ONE table of the platform's words for a broken form shape, in plain English; a surface
    shows the caller's ``wording.form.fields[<name>].invalid`` in its place when set. A missing
    bound reads as the kind's generic sentence. Exhaustive over :class:`FormShapeKind`.
    """
    match kind:
        case FormShapeKind.REQUIRED:
            return "This field is required"
        case FormShapeKind.TYPE:
            return _TYPE_SENTENCES.get(str(params.get("type")), "Enter a value of the expected kind")
        case FormShapeKind.PATTERN:
            return _PATTERN_SENTENCE
        case FormShapeKind.FORMAT:
            return _FORMAT_SENTENCES.get(str(params.get("format")), _PATTERN_SENTENCE)
        case FormShapeKind.MIN_LENGTH:
            bound = params.get("minLength")
            return f"Enter at least {_count(bound, 'character', 'characters')}" if bound is not None else "Enter more"
        case FormShapeKind.MAX_LENGTH:
            bound = params.get("maxLength")
            return f"Enter at most {_count(bound, 'character', 'characters')}" if bound is not None else "Enter less"
        case FormShapeKind.MINIMUM:
            bound = params.get("minimum")
            return f"Enter a number of at least {_number(bound)}" if bound is not None else "Enter a larger number"
        case FormShapeKind.MAXIMUM:
            bound = params.get("maximum")
            return f"Enter a number of at most {_number(bound)}" if bound is not None else "Enter a smaller number"
        case FormShapeKind.MIN_ITEMS:
            bound = params.get("minItems")
            return f"Choose at least {_count(bound, 'option', 'options')}" if bound is not None else "Choose more"
        case FormShapeKind.MAX_ITEMS:
            bound = params.get("maxItems")
            return f"Choose at most {_count(bound, 'option', 'options')}" if bound is not None else "Choose fewer"
        case FormShapeKind.ENUM:
            return "Choose one of the offered options"
        case FormShapeKind.MIN_DATE:
            bound = params.get("minDate")
            return f"Choose a date on or after {bound}" if bound is not None else "Choose a later date"
        case FormShapeKind.MAX_DATE:
            bound = params.get("maxDate")
            return f"Choose a date on or before {bound}" if bound is not None else "Choose an earlier date"
        case FormShapeKind.DATE_UNAVAILABLE:
            return "That date is unavailable, choose another"
        case FormShapeKind.RANGE_ORDER:
            if params.get("by") == "time":
                return "The end time must be on or after the start time"
            return "The end date must be on or after the start date"
        case FormShapeKind.RANGE_SPAN:
            return _span_sentence(params)
        case FormShapeKind.OTHER:
            return _OTHER_SENTENCE
        case _ as unreachable:
            assert_never(unreachable)
