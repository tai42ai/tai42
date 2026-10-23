"""The shape attributes a callback form page's controls carry for its submit script.

Each text, date and number control carries what the page's constant script checks before the
answer leaves the page — the schema ``pattern``, the platform's regex for a typed string
``format``, a range END's start and span, the native number bounds — and, per shape it can
break, the platform's sentence for it (the one table of words, :func:`shape_sentence`), with the
caller's ``wording.form.fields[<name>].invalid`` in their place when set. A ``timeOf`` time carries
the date it is the time of, and the END's time its START's time, so the script orders the two times
of a same-date range. A required control carries, beside the native ``required``, the caller's
``wording.form.fields[<name>].required`` sentence when set, which the script shows in place of the
browser's own prompt.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from typing import Any

from tai42_contract.interactions.models import FormShapeKind
from tai42_kit.interactions.form_vocabulary import FORMAT_PATTERNS, TIME_OF_KEY, drawn_number_bounds

from tai42_skeleton.interactions.form_schema import paired_time
from tai42_skeleton.interactions.form_shape_words import shape_sentence


def _attr(value: Any) -> str:
    # A double-quoted attribute value, HTML-escaped (quotes included).
    return html.escape(str(value), quote=True)


def _msg(kind: FormShapeKind, params: dict[str, Any]) -> str:
    # One ``data-msg-<kind>`` attribute: the platform's sentence for that broken shape.
    return f' data-msg-{kind.value}="{_attr(shape_sentence(kind, params))}"'


def _shape_attrs(prop: dict[str, Any], invalid: str | None, properties: Mapping[str, Any]) -> str:
    """The attributes the submit script checks a control's shapes with, before the answer leaves the page.

    ``data-pattern`` carries the schema ``pattern`` and ``data-format-pattern`` the platform's own
    regex for a typed string ``format`` (``email``/``time``/``date-time``); the script tests each
    with ``new RegExp(...)`` — ECMAScript's unanchored search, JSON Schema's own semantics, never the
    native ``pattern`` attribute (anchored, another regex dialect). A range END carries its START's
    name and its span bounds, and a pair of two ``date-time`` ends marks its END
    ``data-range-instant`` (the instant order is the answer door's). A ``timeOf`` time carries
    ``data-time-of``; the END's time also carries ``data-range-start-time`` (its START's time) and
    the time-order sentence. ``properties`` is the form's properties by name. Each shape the
    control can break carries the platform's sentence for it as ``data-msg-<kind>`` (one owner of
    the words: the platform's one table), and the caller's ``wording.form.fields[<name>].invalid``
    rides ``data-invalid``, which the script shows in their place. A native bound
    (``min``/``max``/``minlength``/``maxlength``) is drawn by its control.
    """
    parts: list[str] = []
    if invalid:
        parts.append(f' data-invalid="{_attr(invalid)}"')
    pattern = prop.get("pattern")
    if isinstance(pattern, str):
        parts.append(f' data-pattern="{_attr(pattern)}"')
        parts.append(_msg(FormShapeKind.PATTERN, {"pattern": pattern}))
    form_format = prop.get("format")
    if form_format in ("email", "time", "date-time"):
        parts.append(f' data-format-pattern="{_attr(FORMAT_PATTERNS[form_format])}"')
        parts.append(_msg(FormShapeKind.FORMAT, {"format": form_format}))
    if prop.get("type") in ("integer", "number"):
        parts.append(_msg(FormShapeKind.TYPE, {"type": prop["type"]}))
    for key, kind in (
        ("minLength", FormShapeKind.MIN_LENGTH),
        ("maxLength", FormShapeKind.MAX_LENGTH),
        ("minimum", FormShapeKind.MINIMUM),
        ("maximum", FormShapeKind.MAXIMUM),
        ("minDate", FormShapeKind.MIN_DATE),
        ("maxDate", FormShapeKind.MAX_DATE),
    ):
        if key in prop:
            parts.append(_msg(kind, {key: prop[key]}))
    parts.append(_range_attrs(prop, properties))
    parts.append(_time_of_attrs(prop, properties))
    return "".join(parts)


def _format_of(properties: Mapping[str, Any], name: str) -> Any:
    prop = properties.get(name)
    return prop.get("format") if isinstance(prop, Mapping) else None


def _range_attrs(prop: dict[str, Any], properties: Mapping[str, Any]) -> str:
    # A range END's shape attributes: its START's name, its span bounds and their sentences, and
    # the instant marker of a pair of two ``date-time`` ends.
    start = prop.get("rangeStart")
    if not isinstance(start, str):
        return ""
    span = {key: prop[key] for key in ("minDays", "maxDays") if isinstance(prop.get(key), int)}
    parts = [f' data-range-start="{_attr(start)}"']
    if prop.get("format") == "date-time" and _format_of(properties, start) == "date-time":
        parts.append(" data-range-instant")
    if "minDays" in span:
        parts.append(f' data-min-days="{span["minDays"]}"')
    if "maxDays" in span:
        parts.append(f' data-max-days="{span["maxDays"]}"')
    parts.append(_msg(FormShapeKind.RANGE_ORDER, {"rangeStart": start, "by": "date"}))
    if span:
        parts.append(_msg(FormShapeKind.RANGE_SPAN, span))
    return "".join(parts)


def _time_of_attrs(prop: dict[str, Any], properties: Mapping[str, Any]) -> str:
    # A ``timeOf`` time's attributes: the date it is the time of; on the END's time also the
    # START's time and the time-order sentence. Empty for any other control.
    date_name = prop.get(TIME_OF_KEY)
    if not isinstance(date_name, str):
        return ""
    parts = [f' data-time-of="{_attr(date_name)}"']
    date_prop = properties.get(date_name)
    start = date_prop.get("rangeStart") if isinstance(date_prop, Mapping) else None
    start_time = paired_time(start, properties) if isinstance(start, str) else None
    if isinstance(start, str) and start_time is not None:
        parts.append(f' data-range-start-time="{_attr(start_time)}"')
        parts.append(_msg(FormShapeKind.RANGE_ORDER, {"rangeStart": start, "by": "time"}))
    return "".join(parts)


def _field_words(field_wording: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    # The caller's ``wording.form.fields[<name>]`` entry, or an empty one when the field has none.
    entry = field_wording.get(name)
    return entry if isinstance(entry, Mapping) else {}


def _word(words: Mapping[str, Any], member: str) -> str | None:
    # One member of a field's words entry (``invalid``, ``required``, ``length_hint``, ...), or None
    # when unset.
    value = words.get(member)
    return value if isinstance(value, str) else None


def _required_attr(is_required: bool, words: Mapping[str, Any]) -> str:
    # The native ``required`` attribute of a required control and, when the caller set the field's
    # ``required`` sentence, the ``data-required`` attribute the submit script shows in place of the
    # browser's own prompt. An optional control carries neither.
    if not is_required:
        return ""
    sentence = _word(words, "required")
    return " required" + (f' data-required="{_attr(sentence)}"' if sentence is not None else "")


def _number_bounds(prop: dict[str, Any]) -> str:
    # The native ``min``/``max`` a number input draws from ``minimum``/``maximum`` (the browser's
    # ``checkValidity`` enforces them; an exclusive bound is left to the answer door). An integer's
    # bounds are whole numbers, since the input steps in whole numbers from its ``min``.
    low, high = drawn_number_bounds(prop)
    attrs = ""
    if low is not None:
        attrs += f' min="{low}"'
    if high is not None:
        attrs += f' max="{high}"'
    return attrs
