"""Form-property → Block Kit input element mapping.

The per-property half of the modal form renderer: it maps one answer-schema property
onto the Block Kit element that collects it — the choice-option objects (each with its
label, submitted value and optional second-line ``description``), the single /
multiple-choice controls, the Yes/No boolean radio, the date/time pickers and their
prefills — and refuses, naming the property, anything the subset cannot express or any
value Slack cannot display. The block/view assembly and the public entry points consume
these builders from :mod:`tai42_channel_slack.forms`.
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal
from typing import Any, assert_never

from tai42_contract.channels import ChannelInputError
from tai42_contract.interactions import FormControl, FormInputMode
from tai42_kit.interactions import RADIO_OPTION_THRESHOLD, drawn_number_bounds

# One fixed action_id per input block: block_id is the field name, so the pair
# ``state.values[<field name>][FIELD_ACTION_ID]`` reads a field's value directly, and
# a field's ``block_actions`` dispatch is recognised by this action_id.
FIELD_ACTION_ID = "tai42_form_field"
_YES_NO_OPTIONS = (("Yes", "true"), ("No", "false"))

# Slack Block Kit caps on a choice control: exceeding one is a loud error, never a truncation.
_MAX_OPTION_TEXT_LEN = 75
_MAX_STATIC_SELECT_OPTIONS = 100
# A select menu's ``placeholder`` is a ``plain_text`` object Slack caps at 150 characters
# (see ``cites.md``). A longer caller label cannot be drawn and is refused, never truncated.
_MAX_SELECT_PLACEHOLDER_LEN = 150
# Slack's ``plain_text_input.max_length`` upper bound (1..3000). A declared ``maxLength`` above
# it cannot be drawn and is refused, never truncated.
_MAX_TEXT_INPUT_LEN = 3000

# Slack's own picker formats: ``initial_date`` is "YYYY-MM-DD", ``initial_time`` is
# "HH:mm" (24-hour hour 00-23, minutes 00-59, two digits each, no seconds).
_SLACK_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLACK_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


class FormSchemaError(ChannelInputError):
    """A form schema (or a submitted value) cannot be mapped to Block Kit.

    A permanent refusal, never a retryable delivery failure.
    """


def _array_items_string(spec: dict[str, Any]) -> bool:
    """Whether ``spec`` is an array whose ``items`` are strings (a multiple-choice field)."""
    items = spec.get("items")
    return isinstance(items, dict) and items.get("type") == "string"


def _options_from_spec(
    name: str, spec: dict[str, Any], per_send_options: list[dict[str, Any]] | None
) -> list[dict[str, Any]] | None:
    """The ``{value, label?, description?}`` choice list for a property, or ``None`` when it has none.

    A per-send list wins (it replaces the schema choices for this send) and rides a string
    or array-of-strings property only — on any other type it is refused naming the property.
    Otherwise the choices come from a string property's ``enum`` or an array's string-items
    ``enum``. A malformed ``enum`` is refused naming the property.
    """
    ptype = spec.get("type")
    if per_send_options is not None:
        if ptype == "string" or (ptype == "array" and _array_items_string(spec)):
            return per_send_options
        raise FormSchemaError(
            f"form schema property {name!r} carries per-send options but its type is {ptype!r}, "
            f"not string or array-of-strings"
        )
    if ptype == "string" and "enum" in spec:
        enum = spec["enum"]
        if not isinstance(enum, list) or not enum:
            raise FormSchemaError(f"form schema property {name!r} enum must be a non-empty list")
        return [{"value": str(choice)} for choice in enum]
    if ptype == "array" and _array_items_string(spec):
        items = spec["items"]
        if "enum" in items:
            enum = items["enum"]
            if not isinstance(enum, list) or not enum:
                raise FormSchemaError(f"form schema property {name!r} items enum must be a non-empty list")
            return [{"value": str(choice)} for choice in enum]
    return None


def _option_block(name: str, label: str, value: str, description: str | None = None) -> dict[str, Any]:
    """One option object: the ``label`` shown, the ``value`` submitted, an optional second line.

    ``description`` renders as the option's second-line ``description`` text; it carries
    answer-relevant content (a time, a price), so a value past Slack's cap is refused naming
    the property, never truncated or dropped.
    """
    if len(label) > _MAX_OPTION_TEXT_LEN:
        raise FormSchemaError(
            f"form schema property {name!r} option {label!r} exceeds {_MAX_OPTION_TEXT_LEN} characters"
        )
    block: dict[str, Any] = {"text": {"type": "plain_text", "text": label}, "value": value}
    if description is not None:
        if len(description) > _MAX_OPTION_TEXT_LEN:
            raise FormSchemaError(
                f"form schema property {name!r} option {label!r} description exceeds {_MAX_OPTION_TEXT_LEN} characters"
            )
        block["description"] = {"type": "plain_text", "text": description}
    return block


def _option_blocks(name: str, options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the option objects for a choice control from a ``{value, label?, description?}`` list.

    An empty list or one past the option cap is refused naming the property; each option's
    label (falling back to its value) is shown, its value submitted, and its second-line
    ``description`` (when present) carried onto the option object.
    """
    if not options:
        raise FormSchemaError(f"form schema property {name!r} option list must be a non-empty list")
    if len(options) > _MAX_STATIC_SELECT_OPTIONS:
        raise FormSchemaError(f"form schema property {name!r} options exceed {_MAX_STATIC_SELECT_OPTIONS}")
    blocks: list[dict[str, Any]] = []
    for option in options:
        value = str(option["value"])
        label = str(option.get("label") or value)
        raw_description = option.get("description")
        description = str(raw_description) if raw_description is not None else None
        blocks.append(_option_block(name, label, value, description))
    return blocks


def _apply_choice_placeholder(name: str, element: dict[str, Any], choice_label: str | None) -> None:
    """Draw the caller's ``choice_label`` as a select element's ``placeholder``.

    ``placeholder`` is a ``plain_text`` object Slack caps at :data:`_MAX_SELECT_PLACEHOLDER_LEN`
    characters; a longer label is refused naming the field, never truncated. ``None`` leaves the
    element with no placeholder, so Slack shows its own default prompt (the documented absent-case).
    """
    if choice_label is None:
        return
    if len(choice_label) > _MAX_SELECT_PLACEHOLDER_LEN:
        raise FormSchemaError(
            f"form field {name!r} choice placeholder exceeds {_MAX_SELECT_PLACEHOLDER_LEN} characters"
        )
    element["placeholder"] = {"type": "plain_text", "text": choice_label}


def _single_choice_element(name: str, options: list[dict[str, Any]], choice_label: str | None = None) -> dict[str, Any]:
    """A ``radio_buttons`` (short list) or ``static_select`` (long list) for a string choice.

    A ``static_select`` carries the caller's ``choice_label`` as its ``placeholder`` when set; a
    ``radio_buttons`` group has no placeholder slot, so a choice label draws none there.
    """
    blocks = _option_blocks(name, options)
    if len(blocks) <= RADIO_OPTION_THRESHOLD:
        return {"type": "radio_buttons", "action_id": FIELD_ACTION_ID, "options": blocks}
    element = {"type": "static_select", "action_id": FIELD_ACTION_ID, "options": blocks}
    _apply_choice_placeholder(name, element, choice_label)
    return element


def _multi_choice_element(name: str, options: list[dict[str, Any]], choice_label: str | None = None) -> dict[str, Any]:
    """A ``checkboxes`` (short list) or ``multi_static_select`` (long list) for an array choice.

    A ``multi_static_select`` carries the caller's ``choice_label`` as its ``placeholder`` when
    set; a ``checkboxes`` group has no placeholder slot, so a choice label draws none there.
    """
    blocks = _option_blocks(name, options)
    if len(blocks) <= RADIO_OPTION_THRESHOLD:
        return {"type": "checkboxes", "action_id": FIELD_ACTION_ID, "options": blocks}
    element = {"type": "multi_static_select", "action_id": FIELD_ACTION_ID, "options": blocks}
    _apply_choice_placeholder(name, element, choice_label)
    return element


def _radio_buttons() -> dict[str, Any]:
    """The Yes/No radio a ``boolean`` property renders as."""
    return {
        "type": "radio_buttons",
        "action_id": FIELD_ACTION_ID,
        "options": [
            {"text": {"type": "plain_text", "text": label}, "value": value} for label, value in _YES_NO_OPTIONS
        ],
    }


def _find_option(options: list[dict[str, Any]], value: str) -> dict[str, Any] | None:
    """The option whose ``value`` matches, or ``None``."""
    return next((option for option in options if option["value"] == value), None)


def _slack_date(name: str, value: Any) -> str:
    """A "YYYY-MM-DD" string Slack's ``initial_date``/``selected_date`` accepts.

    Refuses (naming the field) anything not of that exact shape or not a real calendar date —
    Slack's date picker cannot show any other value.
    """
    if not isinstance(value, str) or not _SLACK_DATE_RE.match(value):
        raise FormSchemaError(f"form field {name!r} date value must be 'YYYY-MM-DD', got {value!r}")
    try:
        datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise FormSchemaError(f"form field {name!r} is not a valid calendar date: {value!r}") from exc
    return value


def _slack_time(name: str, value: Any) -> str:
    """An "HH:mm" (24-hour, no seconds) string Slack's ``initial_time``/``selected_time`` accepts.

    Refuses (naming the field) anything not of that exact shape or out of range. A platform
    ``time`` value carrying seconds ("HH:MM:SS") is valid on the platform but impossible on
    Slack's ``HH:mm`` — refused here, never truncated (truncation would silently drop the
    seconds the input value carried).
    """
    if not isinstance(value, str) or not _SLACK_TIME_RE.match(value):
        raise FormSchemaError(
            f"form field {name!r} time value must be 'HH:mm' (24-hour, no seconds) — "
            f"Slack's time picker cannot show seconds; got {value!r}"
        )
    try:
        datetime.datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise FormSchemaError(f"form field {name!r} is not a valid time of day: {value!r}") from exc
    return value


def _apply_single_initial(name: str, element: dict[str, Any], value: Any) -> None:
    # Prefill a static_select / radio_buttons: a bool picks the Yes/No option, any other value
    # the option whose value matches (a value not among the options is a caller bug, refused).
    if isinstance(value, bool):
        match = _find_option(element["options"], "true" if value else "false")
        if match is not None:
            element["initial_option"] = match
        return
    match = _find_option(element["options"], str(value))
    if match is None:
        raise FormSchemaError(f"form field {name!r} prefilled value {value!r} is not among its options")
    element["initial_option"] = match


def _apply_multi_initial(name: str, element: dict[str, Any], value: Any) -> None:
    # Prefill a checkboxes / multi_static_select from a list: each entry must be among the
    # options (a value outside them is a caller bug, refused naming the field).
    if not isinstance(value, list):
        raise FormSchemaError(
            f"form field {name!r} prefilled value for a multiple-choice control must be a list, got {value!r}"
        )
    matches: list[dict[str, Any]] = []
    for item in value:
        match = _find_option(element["options"], str(item))
        if match is None:
            raise FormSchemaError(f"form field {name!r} prefilled value {item!r} is not among its options")
        matches.append(match)
    if matches:
        element["initial_options"] = matches


def _apply_initial(name: str, element: dict[str, Any], value: Any) -> None:
    """Prefill one control from a per-send value.

    ``initial_value`` for a text/number/email/url input (a list joins one entry per line for a
    free multiple-choice text area), ``initial_date``/``initial_time`` for a date/time picker,
    ``initial_option`` for a single choice (select / radio / the Yes/No boolean),
    ``initial_options`` for a multiple-choice control. A value Slack cannot display, or one
    not among a choice control's options, is a caller bug refused naming the field.
    """
    etype = element["type"]
    if etype in ("plain_text_input", "number_input", "email_text_input", "url_text_input"):
        if isinstance(value, list):
            element["initial_value"] = "\n".join(str(item) for item in value)
        else:
            element["initial_value"] = value if isinstance(value, str) else str(value)
        return
    if etype == "datepicker":
        element["initial_date"] = _slack_date(name, value)
        return
    if etype == "timepicker":
        element["initial_time"] = _slack_time(name, value)
        return
    if etype in ("multi_static_select", "checkboxes"):
        _apply_multi_initial(name, element, value)
        return
    _apply_single_initial(name, element, value)


def _draw_text_length_bounds(name: str, spec: dict[str, Any], element: dict[str, Any]) -> None:
    """Draw a string property's ``maxLength``/``minLength`` onto a ``plain_text_input`` element.

    Sets Slack's ``max_length``/``min_length`` so Slack's own in-modal length check shows and
    enforces the maximum, on both the single-line and the multi-line (``multiline``) text input.
    Slack caps ``max_length`` at :data:`_MAX_TEXT_INPUT_LEN`; a declared ``maxLength`` above it
    cannot be drawn and is refused naming the property — never a silent truncation of the
    author's bound. Only Slack's ``plain_text_input`` carries these bounds; see
    :func:`_string_input_element` for the ``email_text_input``/``url_text_input`` elements, which
    do not.
    """
    maximum = spec.get("maxLength")
    if isinstance(maximum, int) and not isinstance(maximum, bool):
        if maximum > _MAX_TEXT_INPUT_LEN:
            raise FormSchemaError(
                f"form schema property {name!r} maxLength {maximum} exceeds Slack's "
                f"{_MAX_TEXT_INPUT_LEN}-character plain_text_input limit"
            )
        element["max_length"] = maximum
    minimum = spec.get("minLength")
    if isinstance(minimum, int) and not isinstance(minimum, bool):
        element["min_length"] = minimum


def _plain_text_input(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """A single-line ``plain_text_input`` carrying the property's drawn length bounds."""
    element: dict[str, Any] = {"type": "plain_text_input", "action_id": FIELD_ACTION_ID}
    _draw_text_length_bounds(name, spec, element)
    return element


def _string_input_element(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """The text element a plain ``string`` property collects, chosen by its ``inputMode``.

    ``email`` (or a ``format: email``) draws Slack's ``email_text_input`` and ``url`` its ``url_text_input`` — the two
    modal text elements that carry Slack's own built-in check. Those two elements accept no
    ``max_length``/``min_length`` (only ``plain_text_input`` does), so a declared
    ``maxLength``/``minLength`` is not drawn on them and stays enforced at the answer door —
    never silently dropped. ``numeric``/``decimal``/``tel`` have no Slack keyboard hint and
    degrade to a ``plain_text_input``: ``number_input`` is a number field, not a keyboard on a
    string, so a string keeps its typed value verbatim. ``text`` or an absent ``inputMode`` is a
    ``plain_text_input``. Every ``plain_text_input`` branch draws the property's length bounds
    (:func:`_draw_text_length_bounds`). The map is exhaustive over
    :class:`~tai42_contract.interactions.FormInputMode`; a value outside it is refused naming
    the property (the render-door backstop for a record that bypassed the ask-time validator).
    """
    if spec.get("format") == "email":
        # ``format: email`` draws Slack's email element (the subset refuses an ``inputMode`` beside it).
        return {"type": "email_text_input", "action_id": FIELD_ACTION_ID}
    if "inputMode" not in spec:
        return _plain_text_input(name, spec)
    raw = spec["inputMode"]
    try:
        mode = FormInputMode(raw)
    except ValueError as exc:
        raise FormSchemaError(f"form schema property {name!r} has unsupported inputMode {raw!r}") from exc
    match mode:
        case FormInputMode.EMAIL:
            return {"type": "email_text_input", "action_id": FIELD_ACTION_ID}
        case FormInputMode.URL:
            return {"type": "url_text_input", "action_id": FIELD_ACTION_ID}
        case FormInputMode.NUMERIC | FormInputMode.DECIMAL | FormInputMode.TEL:
            # Documented degrade: Slack offers no numeric/phone keyboard hint on a text
            # element, so these keep the plain text input and arrive exactly as typed.
            return _plain_text_input(name, spec)
        case FormInputMode.TEXT:
            return _plain_text_input(name, spec)
    assert_never(mode)


def _textarea_element(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """A multi-line ``plain_text_input`` for a ``control: textarea`` string.

    Draws the declared ``maxLength``/``minLength`` as Slack's ``max_length``/``min_length``,
    the same bounds the single-line input draws (:func:`_draw_text_length_bounds`).
    """
    element: dict[str, Any] = {"type": "plain_text_input", "action_id": FIELD_ACTION_ID, "multiline": True}
    _draw_text_length_bounds(name, spec, element)
    return element


def _control_element(
    name: str,
    spec: dict[str, Any],
    ptype: Any,
    options: list[dict[str, Any]] | None,
    choice_label: str | None = None,
) -> dict[str, Any]:
    """The Slack element a declared ``control`` draws — exhaustive over :class:`FormControl`.

    ``chips`` is a NAMED degrade: Slack has no chip input (its ``Tag`` element is display-only),
    so a chip choice draws the shape's default choice control — ``radio_buttons``/
    ``static_select`` for a single string, ``checkboxes``/``multi_static_select`` for an array
    of strings. ``textarea`` draws a multi-line ``plain_text_input`` and its length bounds; a
    per-send choice list on a textarea string wins for that send (the precedence a choice list
    already has over a format), so the field draws the default choice control instead. The map
    is exhaustive (``assert_never``), so a control added to the platform later is a type error
    here until this channel decides its rendering; a value outside the set is refused naming the
    property (the render-door backstop for a record that bypassed the ask-time validator).
    """
    raw = spec["control"]
    try:
        control = FormControl(raw)
    except ValueError as exc:
        raise FormSchemaError(f"form schema property {name!r} has unsupported control {raw!r}") from exc
    match control:
        case FormControl.CHIPS:
            if options is None:
                raise FormSchemaError(f"form schema property {name!r} has control 'chips' but declares no choice list")
            if ptype == "array":
                return _multi_choice_element(name, options, choice_label)
            return _single_choice_element(name, options, choice_label)
        case FormControl.TEXTAREA:
            if options is not None:
                return _single_choice_element(name, options, choice_label)
            return _textarea_element(name, spec)
    assert_never(control)


def _string_element(
    name: str, spec: dict[str, Any], options: list[dict[str, Any]] | None, choice_label: str | None = None
) -> dict[str, Any]:
    """The element a plain ``string`` property (no declared ``control``) collects.

    A choice list (per-send ``options`` or a schema ``enum``) draws the single-choice control; a
    ``format: date``/``time`` draws the native date/time picker (Slack's picker draws no
    min/max/disabled dates and has no range control, so a declared bound/range is enforced on
    submit — the documented Slack degrade); a ``date-time`` stays text (Slack's datetimepicker
    returns a Unix timestamp, not an RFC 3339 string). Any other string draws the text element
    its ``inputMode`` selects (absent ``inputMode`` is a plain text input).
    """
    if options is not None:
        return _single_choice_element(name, options, choice_label)
    fmt = spec.get("format")
    if fmt == "date":
        return {"type": "datepicker", "action_id": FIELD_ACTION_ID}
    if fmt == "time":
        return {"type": "timepicker", "action_id": FIELD_ACTION_ID}
    return _string_input_element(name, spec)


def _element(
    name: str,
    spec: dict[str, Any],
    per_send_options: list[dict[str, Any]] | None,
    choice_label: str | None = None,
) -> dict[str, Any]:
    ptype = spec.get("type")
    options = _options_from_spec(name, spec, per_send_options)
    if spec.get("control") is not None:
        # A declared control (chips/textarea) chooses the element, respecting the choice-list
        # precedence; its per-control shape rules were enforced at the ask door.
        return _control_element(name, spec, ptype, options, choice_label)
    if ptype == "array":
        if options is not None:
            return _multi_choice_element(name, options, choice_label)
        if not _array_items_string(spec):
            raise FormSchemaError(
                f"form schema property {name!r} is an array whose items are not strings; a channel form "
                f"allows only an array of strings"
            )
        # A free array of strings with no declared choices: a multiline text input the human
        # fills one entry per line (split back into a list on submit).
        return {"type": "plain_text_input", "action_id": FIELD_ACTION_ID, "multiline": True}
    if ptype == "string":
        return _string_element(name, spec, options, choice_label)
    if ptype == "boolean":
        return _radio_buttons()
    if ptype in ("integer", "number"):
        return _number_input(spec, decimal=ptype == "number")
    raise FormSchemaError(f"form schema property {name!r} has unsupported type {ptype!r}")


def _decimal_text(value: float) -> str:
    # A bound in plain decimal notation (``1`` for 1.0, ``100000000000000000000`` for 1e20, never an
    # exponent), the form a ``number_input`` bound string takes.
    if isinstance(value, int):
        return str(value)
    text = format(Decimal(repr(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _number_input(spec: dict[str, Any], *, decimal: bool) -> dict[str, Any]:
    """A ``number_input`` drawing the inclusive ``minimum``/``maximum`` as Slack's ``min_value``/``max_value``.

    Slack takes the bounds as strings. An ``integer`` input admits no decimal, so its bounds are
    the tightest whole numbers within them (``minimum: 1.5`` reads ``2``) — the same whole numbers
    pass. An exclusive bound (``exclusiveMinimum``/``exclusiveMaximum``) or ``multipleOf`` has no
    Slack attribute; the answer door refuses a value breaking it and the modal pins the door's
    sentence under the field.
    """
    element: dict[str, Any] = {"type": "number_input", "action_id": FIELD_ACTION_ID, "is_decimal_allowed": decimal}
    low, high = drawn_number_bounds(spec)
    if low is not None:
        element["min_value"] = _decimal_text(low)
    if high is not None:
        element["max_value"] = _decimal_text(high)
    return element
