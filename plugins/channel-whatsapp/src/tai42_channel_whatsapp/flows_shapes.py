"""A form field's declared shape on a WhatsApp Flow: what Meta checks on the control, what the data endpoint checks.

Meta's Flow controls check some shapes themselves before a screen's footer acts: ``required`` on every
input control, ``pattern`` on a ``TextInput`` drawn with the text or number keyboard (with a mandatory
``helper-text``), ``min-chars``/``max-chars`` on a ``TextInput``, ``max-length`` on a ``TextArea``,
``min/max-selected-items`` on a ``CheckboxGroup``/``ChipsSelector``, and the calendar bounds, excluded
days and range span of a ``CalendarPicker``. Every other shape — an ``email``/``time``/``date-time``
format, a number bound, a ``pattern`` on the email/phone keyboard or a text area, a range split over
two controls, any other keyword — is checked by the platform's one form-shape check at this channel's
data endpoint, so a form carrying one is published endpoint-driven (:func:`field_needs_endpoint`).

A control that can show an error carries Meta's ``error-message`` bound to the screen-data key
``<name>__err``; a ``rangeStart`` pair drawn as one range control binds its START's ``__err``,
the object ``{start-date, end-date}`` carrying each end's text. The endpoint fills each text with
the caller's ``wording.form.fields[<name>].invalid``, else this channel's own word for the broken
shape (:func:`channel_word`, each within Meta's tightest ``error-message`` cap). A shape drawn as
a native ``pattern`` carries the helper text Meta requires: the field's description, else the
caller's ``invalid`` sentence, else :func:`helper_word`, which names the expected form with an
example where the shape has one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple, assert_never

from tai42_contract.interactions import FormShapeKind
from tai42_kit.interactions import FORMAT_PATTERNS

#: The Flow controls Meta lets carry an ``error-message`` (outside a ``Form`` wrapper, as here).
ERROR_MESSAGE_TYPES = frozenset(
    {"TextInput", "TextArea", "Dropdown", "RadioButtonsGroup", "CheckboxGroup", "CalendarPicker"}
)

#: Meta's ``error-message`` cap per control: 30 characters on a text control, 80 elsewhere.
ERROR_MESSAGE_MAX_CHARS = {
    "TextInput": 30,
    "TextArea": 30,
    "Dropdown": 80,
    "RadioButtonsGroup": 80,
    "CheckboxGroup": 80,
    "CalendarPicker": 80,
}

#: The whole-number shape an ``integer`` draws as a native ``pattern`` (Meta's number keyboard admits a decimal).
INTEGER_PATTERN = "^-?[0-9]+$"

#: The sample sentence an ``__err`` screen-data declaration carries (Meta requires an example per key).
ERROR_EXAMPLE = "Not an accepted value"

# The keywords that only describe or draw a field — never a shape a value can break.
_DRAWING_KEYWORDS = frozenset(
    {
        "type",
        "title",
        "description",
        "default",
        "examples",
        "inputMode",
        "control",
        "visibleWhen",
        "$comment",
        "readOnly",
        "writeOnly",
        "deprecated",
    }
)

# The channel's word for a broken ``format``, naming the form the value must take (each within 30).
_FORMAT_WORDS = {
    "email": "Use name@example.com",
    "date": "Use YYYY-MM-DD",
    "time": "Use HH:MM",
    "date-time": "Use YYYY-MM-DDTHH:MM:SS+HH:MM",
}

# The channel's word for a value of the wrong JSON type (each within 30).
_TYPE_WORDS = {"integer": "Enter a whole number", "number": "Enter a number"}

# The helper text a natively checked ``format`` shows when the field has no description and the
# caller no ``invalid`` sentence: the expected form with an example that passes its pattern (within 80).
_FORMAT_HELPERS = {
    "time": "A time as HH:MM, like 09:30",
    "date-time": "Date, time and zone, like 2030-12-31T09:30:00+01:00",
}

# The helper text of the whole-number pattern an ``integer`` draws (within 80).
_TYPE_HELPERS = {"integer": "A whole number, like 42"}

# The text-keyboard input types Meta applies a TextInput ``pattern`` to (absent is the text keyboard).
_PATTERN_INPUT_TYPES = (None, "number")


class NativePattern(NamedTuple):
    """A ``pattern`` Meta checks on a control: the regex, the shape it stands for, and that shape's bound."""

    pattern: str
    kind: FormShapeKind
    params: Mapping[str, Any]


def channel_word(kind: FormShapeKind, params: Mapping[str, Any]) -> str:  # noqa: C901, PLR0912 one exhaustive match over the kinds is the channel's one table of words
    """This channel's own word for a broken shape of ``kind`` — within Meta's 30-character text-control cap.

    Shown in a control's ``error-message`` when the caller set no ``wording.form.fields[<name>].invalid``.
    ``params`` is the broken shape's bound as the form-shape check reports it: a ``format`` word names
    the form the value must take and a ``type`` word the kind of value. Exhaustive over :class:`FormShapeKind`.
    """
    match kind:
        case FormShapeKind.REQUIRED:
            return "This field is required"
        case FormShapeKind.TYPE:
            return _TYPE_WORDS.get(str(params.get("type")), "Enter a valid value")
        case FormShapeKind.FORMAT:
            return _FORMAT_WORDS.get(str(params.get("format")), "Not in the expected form")
        case FormShapeKind.PATTERN:
            return "Not in the expected form"
        case FormShapeKind.MIN_LENGTH:
            return "Too short"
        case FormShapeKind.MAX_LENGTH:
            return "Too long"
        case FormShapeKind.MINIMUM:
            return "Too low"
        case FormShapeKind.MAXIMUM:
            return "Too high"
        case FormShapeKind.MIN_ITEMS:
            return "Choose more options"
        case FormShapeKind.MAX_ITEMS:
            return "Choose fewer options"
        case FormShapeKind.ENUM:
            return "Choose an offered option"
        case FormShapeKind.MIN_DATE:
            return "Choose a later date"
        case FormShapeKind.MAX_DATE:
            return "Choose an earlier date"
        case FormShapeKind.DATE_UNAVAILABLE:
            return "That date is unavailable"
        case FormShapeKind.RANGE_ORDER:
            return "End is before start"
        case FormShapeKind.RANGE_SPAN:
            return "Range length not allowed"
        case FormShapeKind.OTHER:
            return ERROR_EXAMPLE
        case _ as unreachable:
            assert_never(unreachable)


def helper_word(kind: FormShapeKind, params: Mapping[str, Any]) -> str:
    """The helper text a native ``pattern`` of ``kind`` shows when nothing else words it — within Meta's 80.

    A ``time``/``date-time`` format and the whole-number shape show the expected form with an
    example; any other shape shows :func:`channel_word`.
    """
    if kind is FormShapeKind.FORMAT and str(params.get("format")) in _FORMAT_HELPERS:
        return _FORMAT_HELPERS[str(params.get("format"))]
    if kind is FormShapeKind.TYPE and str(params.get("type")) in _TYPE_HELPERS:
        return _TYPE_HELPERS[str(params.get("type"))]
    return channel_word(kind, params)


def native_pattern(prop: dict[str, Any], component: dict[str, Any]) -> NativePattern | None:
    """The :class:`NativePattern` Meta checks on this control, or ``None`` when it checks none.

    Meta applies ``pattern`` to a ``TextInput`` drawn with the text or number keyboard only. The
    schema's own ``pattern`` is drawn first; else an ``integer`` draws the whole-number shape and a
    ``time``/``date-time`` string the platform's format regex — the very regex the answer door
    matches, so the control lets through exactly the shapes the door accepts (the calendar validity
    of a matched value is the endpoint's). Its ``kind`` and ``params`` word the helper text.
    """
    if component.get("type") != "TextInput" or component.get("input-type") not in _PATTERN_INPUT_TYPES:
        return None
    pattern = prop.get("pattern")
    if isinstance(pattern, str):
        return NativePattern(pattern, FormShapeKind.PATTERN, {"pattern": pattern})
    if prop.get("type") == "integer":
        return NativePattern(INTEGER_PATTERN, FormShapeKind.TYPE, {"type": "integer"})
    form_format = prop.get("format")
    if form_format in ("time", "date-time"):
        return NativePattern(FORMAT_PATTERNS[form_format], FormShapeKind.FORMAT, {"format": form_format})
    return None


def _keyword_is_native(keyword: str, prop: dict[str, Any], component: dict[str, Any], merged: bool) -> bool:
    # Whether Meta's control checks ``keyword`` of ``prop`` itself.
    ctype = component.get("type")
    if keyword in ("enum", "items"):
        items = prop.get("items")
        extra_item_keys = set(items) - {"type", "enum"} if isinstance(items, dict) else set()
        return not extra_item_keys
    if keyword == "format":
        return prop.get("format") == "date"
    if keyword == "pattern":
        return native_pattern(prop, component) is not None and isinstance(prop.get("pattern"), str)
    if keyword == "minLength":
        return ctype == "TextInput"
    if keyword == "maxLength":
        return ctype in ("TextInput", "TextArea")
    if keyword in ("minItems", "maxItems"):
        return ctype in ("CheckboxGroup", "ChipsSelector")
    if keyword in ("minDate", "maxDate", "unavailableDates"):
        return ctype == "CalendarPicker" or merged
    if keyword in ("rangeStart", "minDays", "maxDays"):
        return merged
    return False


def field_needs_endpoint(prop: dict[str, Any], component: dict[str, Any], merged: bool) -> bool:
    """Whether a field carries a shape its drawn Meta control does not check natively.

    ``component`` is the field's bare Flow control and ``merged`` whether the field is one end of a
    ``rangeStart`` pair drawn as ONE range control (whose span and order Meta checks). Every keyword
    that only describes or draws the field is ignored; any other keyword is native only when the
    control checks it (see the module docstring).
    """
    return any(
        not _keyword_is_native(keyword, prop, component, merged) for keyword in prop if keyword not in _DRAWING_KEYWORDS
    )


def error_binding(cname: str) -> str:
    """The ``${data.<cname>__err}`` reference a control's ``error-message`` reads (a range control's, an object)."""
    return f"${{data.{cname}__err}}"
