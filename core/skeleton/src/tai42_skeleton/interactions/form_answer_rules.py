"""The rules a submitted form answer is judged by on top of its JSON schema.

Two answer-time rules live here. :func:`effective_answer_schema` shapes the stored schema
for one send: the per-send option lists replace the published ``enum`` and a reaction-fed
choice is checked by type alone. :func:`date_constraint_errors` (and its first-item
sentence, :func:`date_constraint_mismatch`) judges the submitted dates against the
platform date-constraint keys a date property carries -- ``minDate``/``maxDate``,
``unavailableDates``, and a ``rangeStart`` pair's order and ``minDays``/``maxDays`` span,
a same-date pair ordered by its ``timeOf`` times. The keys themselves, and their
build-time rules, are defined by :mod:`~tai42_skeleton.interactions.form_schema`.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterator
from typing import Any

from tai42_contract.interactions.models import FormShapeKind
from tai42_kit.interactions.form_vocabulary import FORM_WEEKDAYS, FORMAT_PATTERNS

from tai42_skeleton.interactions.form_schema import paired_time
from tai42_skeleton.interactions.form_shape_words import shape_sentence

# The two formats that carry a real calendar date, so date constraints (bounds,
# unavailable days, a range pairing) apply to them.
_DATE_FORMATS = ("date", "date-time")

_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_DATE_TIME_RE = re.compile(FORMAT_PATTERNS["date-time"])
_TIME_RE = re.compile(FORMAT_PATTERNS["time"])


def _is_iso_date(value: Any) -> bool:
    """A ``YYYY-MM-DD`` (zero-padded) string naming a real calendar date."""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _prop_is_date(prop: dict[str, Any]) -> bool:
    """Whether ``prop`` is a date-carrying property (a ``string`` with a date ``format``)."""
    return prop.get("type") == "string" and prop.get("format") in _DATE_FORMATS


def _answer_date(value: Any) -> datetime.date | None:
    """The calendar date a submitted date/date-time answer names, or ``None`` when it is not one.

    A ``YYYY-MM-DD`` value yields that date; an RFC 3339 ``date-time`` yields its date part
    (compared against the ``YYYY-MM-DD`` bounds on the date part). The order and span of a range
    of two ``date-time`` ends read the instants in the START's offset instead (:func:`_range_error`).
    """
    if not isinstance(value, str):
        return None
    if _is_iso_date(value):
        return datetime.date.fromisoformat(value)
    try:
        return datetime.datetime.fromisoformat(value.upper()).date()
    except ValueError:
        return None


def _answer_instant(value: Any) -> datetime.datetime | None:
    """The offset-aware instant a submitted RFC 3339 ``date-time`` names, or ``None`` when it is not one."""
    if not isinstance(value, str) or not _DATE_TIME_RE.match(value):
        return None
    try:
        return datetime.datetime.fromisoformat(value.upper())
    except ValueError:
        return None


def _answer_time(value: Any) -> datetime.time | None:
    """The wall-clock time a submitted ``HH:MM[:SS]`` value names, or ``None`` when it is not one."""
    if not isinstance(value, str) or not _TIME_RE.match(value):
        return None
    try:
        return datetime.time.fromisoformat(value)
    except ValueError:
        return None


#: One broken date constraint: the date field, the constraint's kind and the bound it names.
DateConstraintError = tuple[str, FormShapeKind, dict[str, Any]]


def _date_bounds_error(prop: dict[str, Any], the_date: datetime.date) -> tuple[FormShapeKind, dict[str, Any]] | None:
    # The kind and bound of a date outside this property's min/max or on an unavailable day, else None.
    minimum = prop.get("minDate")
    if isinstance(minimum, str) and the_date < datetime.date.fromisoformat(minimum):
        return FormShapeKind.MIN_DATE, {"minDate": minimum}
    maximum = prop.get("maxDate")
    if isinstance(maximum, str) and the_date > datetime.date.fromisoformat(maximum):
        return FormShapeKind.MAX_DATE, {"maxDate": maximum}
    unavailable = prop.get("unavailableDates")
    if isinstance(unavailable, list):
        weekday = FORM_WEEKDAYS[the_date.weekday()]
        iso = the_date.isoformat()
        for entry in unavailable:
            if isinstance(entry, str) and (entry == iso or entry.lower() == weekday):
                return FormShapeKind.DATE_UNAVAILABLE, {"date": iso}
    return None


def _span_error(prop: dict[str, Any], span: int) -> tuple[FormShapeKind, dict[str, Any]] | None:
    # The kind and bounds of an inclusive day-count outside ``minDays``/``maxDays``, else None.
    minimum_days = prop.get("minDays")
    maximum_days = prop.get("maxDays")
    too_short = isinstance(minimum_days, int) and span < minimum_days
    too_long = isinstance(maximum_days, int) and span > maximum_days
    if not (too_short or too_long):
        return None
    bounds = {key: prop[key] for key in ("minDays", "maxDays") if isinstance(prop.get(key), int)}
    return FormShapeKind.RANGE_SPAN, {**bounds, "days": span}


def _instant_range_error(
    prop: dict[str, Any], start_name: str, start_value: Any, end_value: Any
) -> tuple[FormShapeKind, dict[str, Any]] | None:
    # A range of two ``date-time`` ends, judged in one frame, the START's offset: the END instant
    # before the START instant breaks the order, and the span counts the calendar days between the
    # two dates read in that offset (so an in-order range always spans at least one day).
    start_at = _answer_instant(start_value)
    end_at = _answer_instant(end_value)
    if start_at is None or end_at is None:
        return None
    end_local = end_at.astimezone(start_at.tzinfo)
    if end_at < start_at:
        by = "date" if end_local.date() < start_at.date() else "time"
        return FormShapeKind.RANGE_ORDER, {"rangeStart": start_name, "by": by}
    return _span_error(prop, (end_local.date() - start_at.date()).days + 1)


def _range_error(
    name: str, prop: dict[str, Any], answer: dict[str, Any], properties: dict[str, Any]
) -> DateConstraintError | None:
    # The broken order or span of a range END field ``name``, else None (no range, or within bounds).
    # A pair of two ``date-time`` ends is judged by instant; any other pair by each value's written
    # date, its span counted in days. Two ``date`` ends on one date that each carry a readable
    # ``timeOf`` time are then ordered by those times, the error keyed to the END's time.
    start_name = prop.get("rangeStart")
    if not (isinstance(start_name, str) and start_name):
        return None
    start_prop = properties.get(start_name)
    start_format = start_prop.get("format") if isinstance(start_prop, dict) else None
    if prop.get("format") == "date-time" and start_format == "date-time":
        error = _instant_range_error(prop, start_name, answer.get(start_name), answer.get(name))
        return (name, *error) if error is not None else None
    start_date = _answer_date(answer.get(start_name))
    end_date = _answer_date(answer.get(name))
    if start_date is None or end_date is None:
        return None
    if end_date < start_date:
        return name, FormShapeKind.RANGE_ORDER, {"rangeStart": start_name, "by": "date"}
    if end_date == start_date:
        time_error = _time_order_error(name, start_name, answer, properties)
        if time_error is not None:
            return time_error
    error = _span_error(prop, (end_date - start_date).days + 1)
    return (name, *error) if error is not None else None


def _time_order_error(
    end_name: str, start_name: str, answer: dict[str, Any], properties: dict[str, Any]
) -> DateConstraintError | None:
    # A same-date range whose END time is before its START time, keyed to the END's time; None when
    # either end has no paired time, or a time is absent or not a readable ``HH:MM[:SS]`` (the
    # dates alone decide then; the time's own ``format`` check refuses an unreadable value).
    start_time_name = paired_time(start_name, properties)
    end_time_name = paired_time(end_name, properties)
    if start_time_name is None or end_time_name is None:
        return None
    start_time = _answer_time(answer.get(start_time_name))
    end_time = _answer_time(answer.get(end_time_name))
    if start_time is None or end_time is None or end_time >= start_time:
        return None
    return end_time_name, FormShapeKind.RANGE_ORDER, {"rangeStart": start_name, "by": "time"}


def date_constraint_errors(schema: dict[str, Any], answer: dict[str, Any]) -> Iterator[DateConstraintError]:
    """Yield every date field of ``answer`` that breaks a platform date constraint, in schema order.

    For each date property present in the answer: the value must fall within its
    ``minDate``/``maxDate`` (inclusive) and off its ``unavailableDates``. For a range END
    field (one declaring ``rangeStart``) the end must be on or after its start and the
    inclusive day-count between them within ``minDays``/``maxDays``. Two ``date-time`` ends are
    ordered by instant (``by: "time"`` when they share a date in the START's offset); two ``date``
    ends on one date are ordered by their ``timeOf`` times when both are given, and that error is
    keyed to the END's time field (``by: "time"``). A value that is not a date
    is left to the schema's own ``format`` check. Each item is ``(field, kind, params)`` — the
    first broken constraint of that field, its :class:`FormShapeKind` and the bound it names.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    for name, prop in properties.items():
        if not isinstance(prop, dict) or not _prop_is_date(prop):
            continue
        key = str(name)
        the_date = _answer_date(answer.get(key))
        if the_date is None:
            continue
        bounds = _date_bounds_error(prop, the_date)
        if bounds is not None:
            yield key, bounds[0], bounds[1]
            continue
        error = _range_error(key, prop, answer, properties)
        if error is not None:
            yield error


def date_constraint_mismatch(schema: dict[str, Any], answer: dict[str, Any]) -> tuple[str, str] | None:
    """The first date field of ``answer`` that breaks a platform date constraint, as ``(message, field)``.

    The first item of :func:`date_constraint_errors`, with the platform's plain sentence for its
    kind (:func:`~tai42_skeleton.interactions.form_shape_words.shape_sentence`); ``None`` when every
    date field holds.
    """
    for field, kind, params in date_constraint_errors(schema, answer):
        return shape_sentence(kind, params), field
    return None


def effective_answer_schema(schema: dict[str, Any], data: Any, choices: Any = ()) -> dict[str, Any]:
    """Return ``schema`` shaped for answer validation: per-send option lists applied, reaction-fed choices typed-only.

    A per-send list replaces the published ``enum`` for one send, so a submitted value is
    judged against the choices the human was actually shown — EXCEPT for a field named in
    ``choices`` (a reaction may have replaced its list while the form was open): such a
    field has its ``enum`` OMITTED entirely so only its TYPE is checked, membership being
    the consumer's at the ``submitted`` event. ``data`` is the stored :class:`FormData`
    dump (or ``None``); ``choices`` is the reaction-fed-choice field names. Non-mutating
    (shallow copies only).
    """
    options = (data or {}).get("options") if isinstance(data, dict) else None
    choice_set = {str(name) for name in (choices or ())}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema
    if not options and not choice_set:
        return schema
    new_properties = dict(properties)
    for name, option_list in (options or {}).items():
        if name in choice_set:
            # A reaction-fed choice: the send-time list is not stamped (the reaction may
            # replace it); membership is checked by the consumer at submit, not here.
            continue
        prop = new_properties.get(name)
        if not isinstance(prop, dict):
            continue
        enum = [option["value"] for option in option_list]
        if prop.get("type") == "array":
            # An array property carries its choices on ``items``, not the array itself:
            # placing the ``enum`` on the array would demand the whole submitted list
            # equal one option, rejecting every real multi-select answer.
            items = prop.get("items")
            items = items if isinstance(items, dict) else {}
            new_properties[name] = {**prop, "items": {**items, "enum": enum}}
        else:
            new_properties[name] = {**prop, "enum": enum}
    for name in choice_set:
        prop = new_properties.get(name)
        if not isinstance(prop, dict):
            continue
        stripped = {key: value for key, value in prop.items() if key != "enum"}
        if stripped.get("type") == "array" and isinstance(stripped.get("items"), dict):
            stripped = {**stripped, "items": {k: v for k, v in stripped["items"].items() if k != "enum"}}
        new_properties[name] = stripped
    return {**schema, "properties": new_properties}
