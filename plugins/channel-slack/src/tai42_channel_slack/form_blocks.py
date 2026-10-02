"""Form-property → Block Kit input element mapping.

The per-property half of the modal form renderer: it maps one answer-schema property
onto the Block Kit element that collects it — the choice-option objects, the single /
multiple-choice controls, the Yes/No boolean radio, the date/time pickers and their
prefills — and refuses, naming the property, anything the subset cannot express or any
value Slack cannot display. The block/view assembly and the public entry points consume
these builders from :mod:`tai42_channel_slack.forms`.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

from tai42_contract.channels import ChannelInputError

# A short choice list (at or below this count) renders as a radio group / checkbox
# group; a longer one as a single / multi select. One shared threshold for both the
# single-choice (radio vs select) and the multiple-choice (checkboxes vs multi-select)
# split, matching the other channels' renderers.
RADIO_OPTION_THRESHOLD = 5

# One fixed action_id per input block: block_id is the field name, so the pair
# ``state.values[<field name>][FIELD_ACTION_ID]`` reads a field's value directly, and
# a field's ``block_actions`` dispatch is recognised by this action_id.
FIELD_ACTION_ID = "tai42_form_field"
_YES_NO_OPTIONS = (("Yes", "true"), ("No", "false"))

# Slack Block Kit caps on a choice control: exceeding one is a loud error, never a truncation.
_MAX_OPTION_TEXT_LEN = 75
_MAX_STATIC_SELECT_OPTIONS = 100

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
    """The ``{value, label?}`` choice list for a property, or ``None`` when it has none.

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


def _option_block(name: str, label: str, value: str) -> dict[str, Any]:
    """One option object: the ``label`` shown, the ``value`` submitted."""
    if len(label) > _MAX_OPTION_TEXT_LEN:
        raise FormSchemaError(
            f"form schema property {name!r} option {label!r} exceeds {_MAX_OPTION_TEXT_LEN} characters"
        )
    return {"text": {"type": "plain_text", "text": label}, "value": value}


def _option_blocks(name: str, options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the option objects for a choice control from a ``{value, label?}`` list.

    An empty list or one past the option cap is refused naming the property; each option's
    label (falling back to its value) is shown and its value submitted.
    """
    if not options:
        raise FormSchemaError(f"form schema property {name!r} option list must be a non-empty list")
    if len(options) > _MAX_STATIC_SELECT_OPTIONS:
        raise FormSchemaError(f"form schema property {name!r} options exceed {_MAX_STATIC_SELECT_OPTIONS}")
    blocks: list[dict[str, Any]] = []
    for option in options:
        value = str(option["value"])
        label = str(option.get("label") or value)
        blocks.append(_option_block(name, label, value))
    return blocks


def _single_choice_element(name: str, options: list[dict[str, Any]]) -> dict[str, Any]:
    """A ``radio_buttons`` (short list) or ``static_select`` (long list) for a string choice."""
    blocks = _option_blocks(name, options)
    etype = "radio_buttons" if len(blocks) <= RADIO_OPTION_THRESHOLD else "static_select"
    return {"type": etype, "action_id": FIELD_ACTION_ID, "options": blocks}


def _multi_choice_element(name: str, options: list[dict[str, Any]]) -> dict[str, Any]:
    """A ``checkboxes`` (short list) or ``multi_static_select`` (long list) for an array choice."""
    blocks = _option_blocks(name, options)
    etype = "checkboxes" if len(blocks) <= RADIO_OPTION_THRESHOLD else "multi_static_select"
    return {"type": etype, "action_id": FIELD_ACTION_ID, "options": blocks}


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

    ``initial_value`` for a text/number input (a list joins one entry per line for a free
    multiple-choice text area), ``initial_date``/``initial_time`` for a date/time picker,
    ``initial_option`` for a single choice (select / radio / the Yes/No boolean),
    ``initial_options`` for a multiple-choice control. A value Slack cannot display, or one
    not among a choice control's options, is a caller bug refused naming the field.
    """
    etype = element["type"]
    if etype in ("plain_text_input", "number_input"):
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


def _element(name: str, spec: dict[str, Any], per_send_options: list[dict[str, Any]] | None) -> dict[str, Any]:
    ptype = spec.get("type")
    options = _options_from_spec(name, spec, per_send_options)
    if ptype == "array":
        if options is not None:
            return _multi_choice_element(name, options)
        if not _array_items_string(spec):
            raise FormSchemaError(
                f"form schema property {name!r} is an array whose items are not strings; a channel form "
                f"allows only an array of strings"
            )
        # A free array of strings with no declared choices: a multiline text input the human
        # fills one entry per line (split back into a list on submit).
        return {"type": "plain_text_input", "action_id": FIELD_ACTION_ID, "multiline": True}
    if ptype == "string":
        if options is not None:
            return _single_choice_element(name, options)
        fmt = spec.get("format")
        if fmt == "date":
            # Slack's datepicker draws no min/max/disabled dates and has no range control, so a
            # declared bound/range is enforced on submit (the documented Slack degrade), never here.
            return {"type": "datepicker", "action_id": FIELD_ACTION_ID}
        if fmt == "time":
            return {"type": "timepicker", "action_id": FIELD_ACTION_ID}
        # A date-time property stays a text input: Slack's datetimepicker returns a Unix
        # timestamp, not an RFC 3339 string.
        return {"type": "plain_text_input", "action_id": FIELD_ACTION_ID}
    if ptype == "boolean":
        return _radio_buttons()
    if ptype in ("integer", "number"):
        return {"type": "number_input", "action_id": FIELD_ACTION_ID, "is_decimal_allowed": ptype == "number"}
    raise FormSchemaError(f"form schema property {name!r} has unsupported type {ptype!r}")
