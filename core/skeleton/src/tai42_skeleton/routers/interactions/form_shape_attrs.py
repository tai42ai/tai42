"""The shape attributes a callback form page's controls carry for its submit script.

Each text, date and number control carries what the page's constant script checks before the
answer leaves the page — the schema ``pattern``, the platform's regex for a typed string
``format``, a range END's start and span, the native number bounds — and, per shape it can
break, the platform's sentence for it (the one table of words, :func:`shape_sentence`), with the
caller's ``wording.form.fields[<name>].invalid`` in their place when set.
"""

from __future__ import annotations

import html
from typing import Any

from tai42_contract.interactions.models import FormShapeKind
from tai42_kit.interactions.form_vocabulary import FORMAT_PATTERNS, drawn_number_bounds

from tai42_skeleton.interactions.form_shape_words import shape_sentence


def _attr(value: Any) -> str:
    # A double-quoted attribute value, HTML-escaped (quotes included).
    return html.escape(str(value), quote=True)


def _msg(kind: FormShapeKind, params: dict[str, Any]) -> str:
    # One ``data-msg-<kind>`` attribute: the platform's sentence for that broken shape.
    return f' data-msg-{kind.value}="{_attr(shape_sentence(kind, params))}"'


def _shape_attrs(prop: dict[str, Any], invalid: str | None) -> str:
    """The attributes the submit script checks a control's shapes with, before the answer leaves the page.

    ``data-pattern`` carries the schema ``pattern`` and ``data-format-pattern`` the platform's own
    regex for a typed string ``format`` (``email``/``time``/``date-time``); the script tests each
    with ``new RegExp(...)`` — ECMAScript's unanchored search, JSON Schema's own semantics, never the
    native ``pattern`` attribute (anchored, another regex dialect). A range END carries its START's
    name and its span bounds. Each shape the control can break carries the platform's sentence for
    it as ``data-msg-<kind>`` (one owner of the words: the platform's one table), and the caller's
    ``wording.form.fields[<name>].invalid`` rides ``data-invalid``, which the script shows in their
    place. A native bound (``min``/``max``/``minlength``/``maxlength``) is drawn by its control.
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
    parts.append(_range_attrs(prop))
    return "".join(parts)


def _range_attrs(prop: dict[str, Any]) -> str:
    # A range END's shape attributes: its START's name, its span bounds and their sentences.
    start = prop.get("rangeStart")
    if not isinstance(start, str):
        return ""
    span = {key: prop[key] for key in ("minDays", "maxDays") if isinstance(prop.get(key), int)}
    parts = [f' data-range-start="{_attr(start)}"']
    if "minDays" in span:
        parts.append(f' data-min-days="{span["minDays"]}"')
    if "maxDays" in span:
        parts.append(f' data-max-days="{span["maxDays"]}"')
    parts.append(_msg(FormShapeKind.RANGE_ORDER, {"rangeStart": start}))
    if span:
        parts.append(_msg(FormShapeKind.RANGE_SPAN, span))
    return "".join(parts)


def _invalid(field_wording: dict[str, Any], name: str) -> str | None:
    # The caller's ``wording.form.fields[<name>].invalid`` sentence, or None when unset.
    entry = field_wording.get(name)
    value = entry.get("invalid") if isinstance(entry, dict) else None
    return value if isinstance(value, str) else None


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
