"""The platform date keys of a form schema and their build-time rules.

A date property (a ``string`` with ``format`` ``date``/``date-time``) may carry ``minDate``/
``maxDate`` (inclusive ``YYYY-MM-DD`` bounds) and ``unavailableDates`` (``YYYY-MM-DD`` dates and/or
lowercase weekday names). A date RANGE is two date fields: the END names its START in
``rangeStart`` and carries the inclusive day-count bounds ``minDays``/``maxDays``. Each end of a
range of two ``date`` ends may have a time: a ``format: time`` field naming that end in ``timeOf``.

:func:`check_form_date_rules` is the one check of these keys. Every door that carries a form
reaches it (the ask record, the composition rules of a notification and of a conversation part,
and the skeleton's form schema rules), so a nonsensical date key is refused when the form is
built, naming the property, whatever door built it.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Final, cast

__all__ = [
    "DATE_CONSTRAINT_KEYS",
    "FORM_WEEKDAYS",
    "TIME_OF_KEY",
    "check_form_date_rules",
    "check_time_pages",
]

#: The date-constraint keywords a ``date``/``date-time`` property may carry.
DATE_CONSTRAINT_KEYS: Final = ("minDate", "maxDate", "unavailableDates", "rangeStart", "minDays", "maxDays")

#: The lowercase weekday names an ``unavailableDates`` entry may name to exclude a recurring
#: weekday (alongside explicit ``YYYY-MM-DD`` dates), indexed by ``date.weekday()``.
FORM_WEEKDAYS: Final = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

#: The keyword a ``format: time`` field carries to name the range-end date it is the time of. It
#: is not a date constraint (it sits on a time field), so it is not one of
#: :data:`DATE_CONSTRAINT_KEYS`.
TIME_OF_KEY: Final = "timeOf"

# The two formats that carry a real calendar date, so the date keys apply to them.
_DATE_FORMATS = ("date", "date-time")

_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


def _is_iso_date(value: Any) -> bool:
    # A ``YYYY-MM-DD`` (zero-padded) string naming a real calendar date.
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _prop_is_date(prop: dict[str, Any]) -> bool:
    return prop.get("type") == "string" and prop.get("format") in _DATE_FORMATS


def _check_bound_date(name: str, key: str, value: Any) -> None:
    if not _is_iso_date(value):
        raise ValueError(f"form schema property {name!r} {key} must be a YYYY-MM-DD date, got {value!r}")


def _check_date_bounds(name: str, prop: dict[str, Any]) -> None:
    if "minDate" in prop:
        _check_bound_date(name, "minDate", prop["minDate"])
    if "maxDate" in prop:
        _check_bound_date(name, "maxDate", prop["maxDate"])
    if "minDate" in prop and "maxDate" in prop and prop["maxDate"] < prop["minDate"]:
        raise ValueError(
            f"form schema property {name!r} has maxDate {prop['maxDate']!r} before minDate {prop['minDate']!r}"
        )
    if "unavailableDates" in prop:
        entries = prop["unavailableDates"]
        if not isinstance(entries, list):
            raise ValueError(f"form schema property {name!r} unavailableDates must be a list")
        for entry in cast("list[Any]", entries):
            if not (isinstance(entry, str) and (_is_iso_date(entry) or entry.lower() in FORM_WEEKDAYS)):
                raise ValueError(
                    f"form schema property {name!r} unavailableDates entries must be a YYYY-MM-DD date or a "
                    f"weekday name ({', '.join(FORM_WEEKDAYS)}), got {entry!r}"
                )


def _check_range_span(name: str, prop: dict[str, Any]) -> None:
    has_range = "rangeStart" in prop
    if has_range and (not isinstance(prop["rangeStart"], str) or not prop["rangeStart"].strip()):
        raise ValueError(f"form schema property {name!r} rangeStart must name a start date property")
    for span in ("minDays", "maxDays"):
        if span not in prop:
            continue
        if not has_range:
            raise ValueError(f"form schema property {name!r} carries {span} but declares no rangeStart")
        value = prop[span]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"form schema property {name!r} {span} must be a non-negative integer, got {value!r}")
    if "minDays" in prop and "maxDays" in prop and prop["minDays"] > prop["maxDays"]:
        raise ValueError(f"form schema property {name!r} has minDays {prop['minDays']} above maxDays {prop['maxDays']}")


def _check_date_keys(name: str, prop: dict[str, Any]) -> None:
    # Every date key requires a date property, and its value must be well-formed.
    present = [key for key in DATE_CONSTRAINT_KEYS if key in prop]
    if present and not _prop_is_date(prop):
        raise ValueError(
            f"form schema property {name!r} carries date constraints {present} but is not a date property "
            f"(a 'string' with format 'date'/'date-time')"
        )
    _check_date_bounds(name, prop)
    _check_range_span(name, prop)


def _check_range_start(name: str, start: str, properties: dict[str, Any], start_to_end: dict[str, str]) -> None:
    # A ``rangeStart`` names a declared DATE property (not itself) that is a plain start (not
    # another range's END: a chain is ambiguous), and no earlier END names the same start.
    if start == name:
        raise ValueError(f"form schema property {name!r} rangeStart cannot name itself")
    if start not in properties:
        raise ValueError(f"form schema property {name!r} rangeStart names undeclared property {start!r}")
    target = properties[start]
    if not isinstance(target, dict) or not _prop_is_date(cast("dict[str, Any]", target)):
        raise ValueError(f"form schema property {name!r} rangeStart names non-date property {start!r}")
    target_start = cast("dict[str, Any]", target).get("rangeStart")
    if isinstance(target_start, str) and target_start:
        raise ValueError(
            f"form schema property {name!r} rangeStart names {start!r}, which is already paired with {target_start!r}"
        )
    if start in start_to_end:
        raise ValueError(
            f"form schema property {name!r} rangeStart names {start!r}, which is already paired "
            f"with {start_to_end[start]!r}"
        )
    start_to_end[start] = name


def _object_properties(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    # The top-level properties that are objects, by name.
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {
        str(name): cast("dict[str, Any]", prop)
        for name, prop in cast("dict[Any, Any]", properties).items()
        if isinstance(prop, dict)
    }


def _range_other_ends(props: dict[str, dict[str, Any]]) -> dict[str, str]:
    # Each range end by name, mapped to the other end of its pair.
    other: dict[str, str] = {}
    for name, prop in props.items():
        start = prop.get("rangeStart")
        if isinstance(start, str) and start:
            other[start] = name
            other[name] = start
    return other


def _check_time_of(
    name: str,
    prop: dict[str, Any],
    declared: dict[str, Any],
    other_end: dict[str, str],
    time_of_date: dict[str, str],
) -> None:
    # A ``timeOf`` time names a ``date`` end of a range whose other end is a ``date`` too, holds
    # that date's one time, and shows exactly when its date shows.
    if prop.get("type") != "string" or prop.get("format") != "time":
        raise ValueError(
            f"form schema property {name!r} carries {TIME_OF_KEY} but is not a time property "
            f"(a 'string' with format 'time')"
        )
    date_name = prop[TIME_OF_KEY]
    if not isinstance(date_name, str) or not date_name.strip():
        raise ValueError(f"form schema property {name!r} {TIME_OF_KEY} must name a range end date property")
    if date_name not in declared:
        raise ValueError(f"form schema property {name!r} {TIME_OF_KEY} names undeclared property {date_name!r}")
    date_prop = declared[date_name]
    date_format = cast("dict[str, Any]", date_prop).get("format") if isinstance(date_prop, dict) else None
    if date_format == "date-time":
        raise ValueError(
            f"form schema property {name!r} {TIME_OF_KEY} names {date_name!r}, a 'date-time' property that "
            "already carries its own time"
        )
    if not isinstance(date_prop, dict) or not _prop_is_date(cast("dict[str, Any]", date_prop)):
        raise ValueError(
            f"form schema property {name!r} {TIME_OF_KEY} names {date_name!r}, which is not a 'date' property"
        )
    if date_name not in other_end:
        raise ValueError(
            f"form schema property {name!r} {TIME_OF_KEY} names {date_name!r}, which is not an end of a date range "
            "(a rangeStart pair)"
        )
    other = other_end[date_name]
    if cast("dict[str, Any]", declared[other]).get("format") == "date-time":
        raise ValueError(
            f"form schema property {name!r} {TIME_OF_KEY} names {date_name!r}, whose range's other end {other!r} "
            "is a 'date-time' property: a time without an offset cannot be ordered against it"
        )
    if date_name in time_of_date:
        raise ValueError(
            f"form schema property {name!r} {TIME_OF_KEY} names {date_name!r}, which already has the time "
            f"{time_of_date[date_name]!r}"
        )
    time_of_date[date_name] = name
    if prop.get("visibleWhen") != cast("dict[str, Any]", date_prop).get("visibleWhen"):
        raise ValueError(f"form schema property {name!r} must carry the same visibleWhen as its date {date_name!r}")


def check_form_date_rules(schema: dict[str, Any]) -> None:
    """Refuse a form schema whose date keys make no sense, naming the property.

    Each top-level property that is an object is checked: a date key on a non-date property, a
    malformed or reversed ``minDate``/``maxDate``, a malformed ``unavailableDates`` entry, a blank
    ``rangeStart``, a span key without ``rangeStart``, a negative or non-integer span, ``minDays``
    above ``maxDays``; then each ``rangeStart`` must name another declared date property that is
    not itself a range END and that no other END names. Last, each ``timeOf`` must sit on a
    ``format: time`` field and name a ``format: date`` end of a range whose other end is a
    ``date`` too, at most one time per date, with the same ``visibleWhen`` as that date. The page
    rule of a time is :func:`check_time_pages`. Raises ``ValueError``.
    """
    props = _object_properties(schema)
    for name, prop in props.items():
        _check_date_keys(name, prop)
    declared = cast("dict[str, Any]", schema.get("properties") or {})
    start_to_end: dict[str, str] = {}
    for name, prop in props.items():
        start = prop.get("rangeStart")
        if isinstance(start, str) and start:
            _check_range_start(name, start, declared, start_to_end)
    other_end = _range_other_ends(props)
    time_of_date: dict[str, str] = {}
    for name, prop in props.items():
        if TIME_OF_KEY in prop:
            _check_time_of(name, prop, declared, other_end, time_of_date)


def check_time_pages(schema: dict[str, Any], page_fields: list[tuple[str, list[str]]]) -> None:
    """Refuse a ``timeOf`` time placed on another page than its date, naming the property.

    ``page_fields`` is each page's ``(title, fields)``, in page order. A time and its date are
    drawn together, so both sit on one page. Pages are told apart by their position (titles may
    repeat), and the refusal names each page by its 1-based number and title. A ``timeOf`` that
    names no field on any page is left to :func:`check_form_date_rules`. Raises ``ValueError``.
    """
    page_of = {field: index for index, (_title, fields) in enumerate(page_fields) for field in fields}

    def page_label(index: int) -> str:
        return f"{index + 1} {page_fields[index][0]!r}"

    for name, prop in _object_properties(schema).items():
        date_name = prop.get(TIME_OF_KEY)
        if not isinstance(date_name, str) or date_name not in page_of or name not in page_of:
            continue
        if page_of[name] != page_of[date_name]:
            raise ValueError(
                f"form schema property {name!r} is the time of {date_name!r} and must sit on the same page "
                f"(it is on page {page_label(page_of[name])}, its date on page {page_label(page_of[date_name])})"
            )
