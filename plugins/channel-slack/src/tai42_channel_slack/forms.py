"""Schema → Block Kit mapping for ``answer_format == "form"`` questions.

Three shapes are built here, all from the same JSON answer schema (a top-level
``{"type": "object", "properties": {...}, "required": [...]}``) plus the per-send
layout the ask carried (``data``/``pages``/``reactions``):

* the outbound message blocks — a section carrying the question and one button
  (:data:`FORM_OPEN_ACTION_ID`) whose ``value`` is the interaction id;
* the modal view (:data:`FORM_SUBMIT_CALLBACK_ID`) opened on the button click,
  one input block per property, interleaved with display blocks and a review step;
* the answer dict extracted and coerced from a ``view_submission`` state.

Supported property subset (identical to the callback door's own form renderer, so
a modal answer validates there): ``string`` → ``plain_text_input``; ``string``
with an ``enum`` (or per-send ``options``) → ``radio_buttons`` at or below
:data:`RADIO_OPTION_THRESHOLD` choices, else ``static_select``; ``string``+
``format: date`` → ``datepicker`` (Slack's picker draws NO min/max/disabled dates
and has NO range control, so a declared date bound or range is enforced on SUBMIT,
never drawn — the documented Slack degrade); ``string``+``format: time`` →
``timepicker`` ("HH:mm", 24-hour, no seconds); ``string``+``format: date-time`` →
``plain_text_input`` (Slack's ``datetimepicker`` returns a Unix timestamp, not an
RFC 3339 string, so ``date-time`` stays text); an ``array`` of strings → a
``checkboxes`` group at or below the threshold, else a ``multi_static_select``,
over the items' ``enum`` or per-send ``options`` (a free array with no choices is a
multiline ``plain_text_input``, one entry per line); ``boolean`` → ``radio_buttons``
(Yes/No → ``true``/``false``); ``integer``/``number`` → ``number_input``. Anything
else, or a value past a Slack cap, raises :class:`FormSchemaError` naming the
property — never a silently dropped or truncated field.

Per-send enrichment rides the same mapping: a ``values`` map prefills each named
property's control; an ``options`` map supplies a per-send choice list (its labels
shown, its values submitted) replacing a string/array property's ``enum`` for this
send.

Display, review and conditionals (capabilities D and B) ride the ``pages`` layout.
A page's ordered ``display`` blocks render as ``header`` (heading), ``section``
(body, mrkdwn) and ``image`` blocks interleaved with its inputs; a ``review`` page
renders a generic readback ``section`` of the values entered so far. A property's
``visibleWhen`` predicate is evaluated HERE (platform logic, never a consumer call)
against the known values: a field hidden by it is omitted, and the controlling
field carries ``dispatch_action`` so the interactivity door can re-render the modal
on a change.

A reacting form carries ``reactions`` triggers: a ``field_changed``
field carries ``dispatch_action`` so its change reaches the reaction door, and a
reaction's returned update (set values, replaced option lists, per-field errors,
filled display slots) is applied on the next ``views.update``. The accumulated
reaction state (replaced option lists, filled display slots) rides the view's
``private_metadata`` alongside the interaction id, so it survives across re-renders.
"""

from __future__ import annotations

import datetime
import json
import math
import re
from typing import Any

from tai42_contract.channels import ChannelInputError

# The message button and the modal it opens.
FORM_OPEN_ACTION_ID = "tai42_form_open"
FORM_SUBMIT_CALLBACK_ID = "tai42_form_submit"
# One fixed action_id per input block: block_id is the field name, so the pair
# ``state.values[<field name>][FIELD_ACTION_ID]`` reads a field's value directly, and
# a field's ``block_actions`` dispatch is recognised by this action_id.
FIELD_ACTION_ID = "tai42_form_field"
_BUTTON_LABEL = "Fill form"
_MODAL_TITLE = "Please respond"
_SUBMIT_LABEL = "Submit"
_CLOSE_LABEL = "Cancel"
_YES_NO_OPTIONS = (("Yes", "true"), ("No", "false"))

# A short choice list (at or below this count) renders as a radio group / checkbox
# group; a longer one as a single / multi select. One shared threshold for both the
# single-choice (radio vs select) and the multiple-choice (checkboxes vs multi-select)
# split, matching the other channels' renderers.
RADIO_OPTION_THRESHOLD = 5

# Slack Block Kit caps: exceeding one is a loud error, never a truncation.
_MAX_MODAL_BLOCKS = 100
_MAX_LABEL_LEN = 2000
_MAX_SECTION_TEXT_LEN = 3000
_MAX_OPTION_TEXT_LEN = 75
_MAX_STATIC_SELECT_OPTIONS = 100
_MAX_BUTTON_VALUE_LEN = 2000
# A ``header`` block's plain_text cap — the surface a form page's title (and a
# display heading) renders on.
_MAX_HEADER_LEN = 150
# The ``private_metadata`` string cap Slack enforces on a view.
_MAX_PRIVATE_METADATA_LEN = 3000

# A triggering text input dispatches its ``block_actions`` when the human presses
# enter (a select/radio/checkbox/datepicker dispatches on selection with no config).
_TEXT_DISPATCH_CONFIG = {"trigger_actions_on": ["on_enter_pressed"]}

# The line a review page shows before any value has been entered.
_REVIEW_EMPTY_TEXT = "No answers to review yet."

# Slack's own picker formats: ``initial_date`` is "YYYY-MM-DD", ``initial_time`` is
# "HH:mm" (24-hour hour 00-23, minutes 00-59, two digits each, no seconds).
_SLACK_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLACK_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


class FormSchemaError(ChannelInputError):
    """A form schema (or a submitted value) cannot be mapped to Block Kit.

    A permanent refusal, never a retryable delivery failure.
    """


def _properties(schema: dict[str, Any]) -> dict[str, Any]:
    """The validated non-empty ``properties`` map of a top-level object schema."""
    if not isinstance(schema, dict):
        raise FormSchemaError("form schema must be an object")
    if schema.get("type") != "object":
        raise FormSchemaError(f"form schema top-level type must be 'object', got {schema.get('type')!r}")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise FormSchemaError("form schema must carry a non-empty object 'properties' map")
    return properties


def _required_set(schema: dict[str, Any]) -> set[str]:
    required = schema.get("required", [])
    if not isinstance(required, list):
        raise FormSchemaError("form schema 'required' must be a list when present")
    return {str(name) for name in required}


def first_field_name(schema: dict[str, Any]) -> str:
    """The first property name.

    The fallback block a door-side error is pinned under when the door names no locatable field.
    """
    return next(iter(_properties(schema)))


def is_declared_field(schema: dict[str, Any], name: str) -> bool:
    """Whether ``name`` is a declared property of the schema.

    A real input block_id (block_id == field name) a door-side error can be pinned under.
    """
    return name in _properties(schema)


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


def _input_block(
    name: str,
    spec: Any,
    is_required: bool,
    value: Any = None,
    per_send_options: list[dict[str, Any]] | None = None,
    *,
    dispatch: bool = False,
) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise FormSchemaError(f"form schema property {name!r} must be an object")
    label = str(spec.get("title") or name)
    if len(label) > _MAX_LABEL_LEN:
        raise FormSchemaError(f"form schema property {name!r} label exceeds {_MAX_LABEL_LEN} characters")
    element = _element(name, spec, per_send_options)
    if value is not None:
        _apply_initial(name, element, value)
    if dispatch and element["type"] in ("plain_text_input", "number_input"):
        # A text/number input dispatches on enter; a selection control dispatches on its own.
        element["dispatch_action_config"] = _TEXT_DISPATCH_CONFIG
    block: dict[str, Any] = {
        "type": "input",
        "block_id": name,
        "label": {"type": "plain_text", "text": label},
        "element": element,
    }
    if dispatch:
        # The change of a reaction/conditional trigger field emits a ``block_actions`` the
        # interactivity door routes to the reaction door / a conditional re-render.
        block["dispatch_action"] = True
    if not is_required:
        # Slack input blocks are required unless flagged optional.
        block["optional"] = True
    return block


def _question_section(question: str) -> dict[str, Any]:
    if len(question) > _MAX_SECTION_TEXT_LEN:
        raise FormSchemaError(f"form question exceeds {_MAX_SECTION_TEXT_LEN} characters")
    return {"type": "section", "text": {"type": "plain_text", "text": question}}


def _page_header(title: str) -> dict[str, Any]:
    """One form page's title as a Block Kit ``header``.

    A bold titled group, Slack's stand-in for a step (a modal has no native multi-step).
    """
    if len(title) > _MAX_HEADER_LEN:
        raise FormSchemaError(f"form page title {title!r} exceeds {_MAX_HEADER_LEN} characters")
    return {"type": "header", "text": {"type": "plain_text", "text": title}}


def _evaluate_visible_when(predicate: dict[str, Any], values: dict[str, Any]) -> bool:
    # Whether a property with ``predicate`` is VISIBLE given the known ``values`` — the SAME
    # platform rule the answer facet enforces at submit, evaluated here so the modal shows/hides
    # without ever calling the consumer. An absent controlling value reads as unset.
    controlling = values.get(predicate["field"])
    if "equals" in predicate:
        return controlling == predicate["equals"]
    if "in" in predicate:
        return controlling in predicate["in"]
    # notEmpty
    return controlling not in (None, "", [], {})


def _hidden_fields(properties: dict[str, Any], values: dict[str, Any]) -> set[str]:
    # The properties HIDDEN by their ``visibleWhen`` predicate for the known ``values`` (a
    # property with no predicate is always visible).
    hidden: set[str] = set()
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        predicate = prop.get("visibleWhen")
        if isinstance(predicate, dict) and not _evaluate_visible_when(predicate, values):
            hidden.add(str(name))
    return hidden


def _trigger_fields(properties: dict[str, Any], reactions: dict[str, Any] | None) -> set[str]:
    # The fields whose change must emit a ``block_actions``: a field another property's
    # ``visibleWhen`` depends on (so the modal re-renders the show/hide), and a reaction's
    # declared ``field_changed`` field (so the reaction door runs).
    triggers: set[str] = set()
    for prop in properties.values():
        if not isinstance(prop, dict):
            continue
        predicate = prop.get("visibleWhen")
        if isinstance(predicate, dict) and isinstance(predicate.get("field"), str):
            triggers.add(predicate["field"])
    if reactions:
        for field in reactions.get("field_changed") or []:
            triggers.add(str(field))
    return triggers


def _readback_value(value: Any) -> str:
    # One review-line rendering of a submitted value: Yes/No for a bool, comma-joined for a
    # list, the string otherwise.
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _readback_section(properties: dict[str, Any], values: dict[str, Any], hidden: set[str]) -> dict[str, Any]:
    # A review page's generic readback of the values entered so far: one ``*label*: value``
    # line per visible, filled field. A field hidden by its predicate is omitted (it is not
    # part of the answer); when nothing is filled yet the placeholder line is shown.
    lines: list[str] = []
    for name, spec in properties.items():
        if name in hidden or name not in values:
            continue
        label = str((spec.get("title") if isinstance(spec, dict) else None) or name)
        lines.append(f"*{label}*: {_readback_value(values[name])}")
    text = "\n".join(lines) if lines else _REVIEW_EMPTY_TEXT
    if len(text) > _MAX_SECTION_TEXT_LEN:
        raise FormSchemaError(f"form review readback exceeds {_MAX_SECTION_TEXT_LEN} characters")
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _display_block(block: dict[str, Any], display_values: dict[str, Any]) -> dict[str, Any] | None:
    # Render one ordered display block: heading→header, body→section (mrkdwn), image→image
    # block. A slotted block takes its content from ``display_values`` (a reaction fills it);
    # a slotted block with no value yet renders nothing (not an error — it fills on a reaction).
    # An image with no drawable source degrades to its alt text as a context block.
    kind = block.get("kind")
    slot = block.get("slot")
    if kind == "image":
        src = display_values.get(slot) if isinstance(slot, str) and slot in display_values else block.get("src")
        alt = str(block.get("alt") or "")
        if src:
            return {"type": "image", "image_url": str(src), "alt_text": alt or " "}
        if alt:
            return {"type": "context", "elements": [{"type": "mrkdwn", "text": alt}]}
        return None
    text = display_values.get(slot) if isinstance(slot, str) and slot in display_values else block.get("text")
    if not text:
        return None
    text = str(text)
    if kind == "heading":
        if len(text) > _MAX_HEADER_LEN:
            raise FormSchemaError(f"form display heading exceeds {_MAX_HEADER_LEN} characters")
        return {"type": "header", "text": {"type": "plain_text", "text": text}}
    if len(text) > _MAX_SECTION_TEXT_LEN:
        raise FormSchemaError(f"form display body exceeds {_MAX_SECTION_TEXT_LEN} characters")
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _error_context(name: str, message: Any) -> dict[str, Any]:
    # A per-field error a reaction returned, shown as a context line under the field's input
    # block (a re-rendered modal cannot carry a native input error, so it rides here).
    text = str(message)
    if len(text) > _MAX_SECTION_TEXT_LEN:
        raise FormSchemaError(f"form field {name!r} error message exceeds {_MAX_SECTION_TEXT_LEN} characters")
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": f":warning: {text}"}]}


def _field_group(
    name: str,
    properties: dict[str, Any],
    required: set[str],
    values: dict[str, Any] | None,
    options: dict[str, list[dict[str, Any]]],
    hidden: set[str],
    triggers: set[str],
    field_errors: dict[str, Any],
) -> list[dict[str, Any]]:
    # One field's blocks: its input block (prefilled, with a dispatch flag when it is a
    # reaction/conditional trigger), plus a context block for any reaction error on it. A
    # field hidden by its predicate contributes nothing; an unknown field is refused.
    spec = properties.get(name)
    if spec is None:
        raise FormSchemaError(f"form page names unknown property {name!r}")
    if name in hidden:
        return []
    prefill = values.get(name) if values is not None else None
    block = _input_block(name, spec, name in required, prefill, options.get(name), dispatch=name in triggers)
    group = [block]
    if name in field_errors:
        group.append(_error_context(name, field_errors[name]))
    return group


def _page_blocks(
    page: dict[str, Any],
    properties: dict[str, Any],
    required: set[str],
    values: dict[str, Any] | None,
    options: dict[str, list[dict[str, Any]]],
    hidden: set[str],
    triggers: set[str],
    field_errors: dict[str, Any],
    display_values: dict[str, Any],
) -> list[dict[str, Any]]:
    # One page: its title header, its ordered display blocks, then either a review readback
    # (a review page) or its input fields (an input page).
    blocks: list[dict[str, Any]] = [_page_header(str(page["title"]))]
    for display in page.get("display") or []:
        rendered = _display_block(display, display_values)
        if rendered is not None:
            blocks.append(rendered)
    if page.get("kind") == "review":
        blocks.append(_readback_section(properties, values or {}, hidden))
        return blocks
    for field in page["fields"]:
        blocks.extend(_field_group(str(field), properties, required, values, options, hidden, triggers, field_errors))
    return blocks


def build_modal_blocks(
    schema: dict[str, Any],
    values: dict[str, Any] | None = None,
    options: dict[str, list[dict[str, Any]]] | None = None,
    pages: list[dict[str, Any]] | None = None,
    *,
    reactions: dict[str, Any] | None = None,
    display_values: dict[str, Any] | None = None,
    field_errors: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The input/display blocks of a form modal.

    Each property renders one prefilled input block (per-send ``options`` build its choice
    list). With ``pages`` the blocks are grouped under a ``header`` per page, interleaved with
    the page's ordered ``display`` blocks, and a ``review`` page shows a generic readback of
    the entered values; without ``pages`` the properties render in schema order (no display).
    A property's ``visibleWhen`` predicate, evaluated here against ``values`` (``None`` =
    evaluate against nothing, so every field shows — the cap upper bound and the show-all
    base), hides a field; a reaction/conditional trigger field carries ``dispatch_action``; a
    reaction's ``display_values`` fill display slots and its ``field_errors`` render as context
    lines. Raises :class:`FormSchemaError` on any property this mapping cannot express or any
    per-send extra it cannot map.
    """
    options = options or {}
    display_values = display_values or {}
    field_errors = field_errors or {}
    required = _required_set(schema)
    properties = _properties(schema)
    hidden = _hidden_fields(properties, values) if values is not None else set()
    triggers = _trigger_fields(properties, reactions)
    if pages is not None:
        blocks: list[dict[str, Any]] = []
        for page in pages:
            blocks.extend(
                _page_blocks(
                    page, properties, required, values, options, hidden, triggers, field_errors, display_values
                )
            )
        return blocks
    blocks = []
    for name in properties:
        blocks.extend(_field_group(str(name), properties, required, values, options, hidden, triggers, field_errors))
    return blocks


def _encode_private_metadata(
    interaction_id: str,
    metadata_options: dict[str, list[dict[str, Any]]] | None = None,
    metadata_display: dict[str, Any] | None = None,
) -> str:
    """The view's ``private_metadata`` string: the interaction id plus the accumulated reaction state.

    The interaction id is the correlation key every ``view_submission``/``block_actions``
    carries back; the reaction-supplied option lists and display slot values ride alongside it
    so they survive a re-render (Slack echoes only entered VALUES in the view state). Raises
    :class:`FormSchemaError` when the encoded state exceeds Slack's ``private_metadata`` cap —
    never a silent truncation.
    """
    payload: dict[str, Any] = {"id": interaction_id}
    if metadata_options:
        payload["options"] = metadata_options
    if metadata_display:
        payload["display"] = metadata_display
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded) > _MAX_PRIVATE_METADATA_LEN:
        raise FormSchemaError(f"form modal private_metadata exceeds {_MAX_PRIVATE_METADATA_LEN} characters")
    return encoded


def decode_private_metadata(raw: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """The ``(interaction_id, reaction_options, reaction_display)`` a view's ``private_metadata`` carries.

    Raises :class:`ValueError` when the metadata is missing or malformed — a view the plugin
    did not stamp is a loud bug, never a silent default.
    """
    if not isinstance(raw, str) or not raw:
        raise ValueError("form view carried no private_metadata")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError("form view private_metadata is not decodable JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("form view private_metadata must be a JSON object")  # noqa: TRY004 a malformed view is a loud ValueError, not a TypeError
    interaction_id = data.get("id")
    if not isinstance(interaction_id, str) or not interaction_id:
        raise ValueError("form view private_metadata carries no interaction id")
    raw_options = data.get("options")
    raw_display = data.get("display")
    options: dict[str, Any] = raw_options if isinstance(raw_options, dict) else {}
    display: dict[str, Any] = raw_display if isinstance(raw_display, dict) else {}
    return interaction_id, options, display


def build_modal_view(
    interaction_id: str,
    question: str,
    schema: dict[str, Any],
    values: dict[str, Any] | None = None,
    options: dict[str, list[dict[str, Any]]] | None = None,
    pages: list[dict[str, Any]] | None = None,
    *,
    reactions: dict[str, Any] | None = None,
    display_values: dict[str, Any] | None = None,
    field_errors: dict[str, Any] | None = None,
    metadata_options: dict[str, list[dict[str, Any]]] | None = None,
    metadata_display: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``views.open``/``views.update`` modal: the question as a section, then the blocks.

    The input/display/review blocks are built with the per-send prefill, option lists, display
    slots and any reaction errors; ``private_metadata`` carries the interaction id plus the
    accumulated reaction state (``metadata_options``/``metadata_display``) back on the next
    interaction.
    """
    blocks = [
        _question_section(question),
        *build_modal_blocks(
            schema,
            values,
            options,
            pages,
            reactions=reactions,
            display_values=display_values,
            field_errors=field_errors,
        ),
    ]
    if len(blocks) > _MAX_MODAL_BLOCKS:
        raise FormSchemaError(f"form modal exceeds {_MAX_MODAL_BLOCKS} blocks")
    return {
        "type": "modal",
        "callback_id": FORM_SUBMIT_CALLBACK_ID,
        "private_metadata": _encode_private_metadata(interaction_id, metadata_options, metadata_display),
        "title": {"type": "plain_text", "text": _MODAL_TITLE},
        "submit": {"type": "plain_text", "text": _SUBMIT_LABEL},
        "close": {"type": "plain_text", "text": _CLOSE_LABEL},
        "blocks": blocks,
    }


def validate_form_schema(schema: dict[str, Any], question: str) -> None:
    """Enforce the ask-time-knowable Block Kit caps at ask-time.

    Raises :class:`FormSchemaError` naming the offending property/limit on any violation.

    Covers every limit knowable before delivery: the question-text section cap, the supported
    property subset, the per-label cap, the option count and per-option text caps, and the
    modal's 100-block cap. The per-send layout (pages/display) and the button-value cap depend
    on the per-send interaction, not the schema or question, so they stay at delivery. Every
    field is counted shown (``values`` left unset), the modal's upper bound.
    """
    if len(question) > _MAX_SECTION_TEXT_LEN:
        raise FormSchemaError(f"form question exceeds {_MAX_SECTION_TEXT_LEN} characters")
    blocks = build_modal_blocks(schema)
    # +1 for the question section the modal view prepends before the input blocks.
    if len(blocks) + 1 > _MAX_MODAL_BLOCKS:
        raise FormSchemaError(f"form modal exceeds {_MAX_MODAL_BLOCKS} blocks")


def build_message_blocks(question: str, interaction_id: str) -> list[dict[str, Any]]:
    """The ``chat.postMessage`` blocks: the question section plus a single button.

    The button's ``value`` is the interaction id (read back on ``block_actions``).
    """
    if len(interaction_id) > _MAX_BUTTON_VALUE_LEN:
        raise FormSchemaError(f"interaction id exceeds the {_MAX_BUTTON_VALUE_LEN}-character button value cap")
    return [
        _question_section(question),
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": FORM_OPEN_ACTION_ID,
                    "value": interaction_id,
                    "text": {"type": "plain_text", "text": _BUTTON_LABEL},
                }
            ],
        },
    ]


def _raw_value(entry: Any) -> str | None:
    """The submitted string of one scalar field, or ``None`` when it was left empty."""
    if not isinstance(entry, dict):
        return None
    kind = entry.get("type")
    if kind in ("plain_text_input", "number_input"):
        value = entry.get("value")
        return value if isinstance(value, str) and value != "" else None
    if kind in ("datepicker", "timepicker"):
        # Slack keys the chosen value by element (``string | null``); an empty picker is
        # an unfilled field, like an empty text input. The ISO string returns unchanged.
        value = entry.get("selected_date" if kind == "datepicker" else "selected_time")
        return value if isinstance(value, str) and value != "" else None
    if kind in ("static_select", "radio_buttons"):
        selected = entry.get("selected_option")
        if isinstance(selected, dict):
            value = selected.get("value")
            return value if isinstance(value, str) else None
        return None
    return None


def _array_value(entry: Any) -> list[str]:
    """The submitted list of one array field — an empty list when it was left unfilled.

    A ``multi_static_select``/``checkboxes`` field reads its ``selected_options`` values; a
    free multiple-choice ``plain_text_input`` splits its text one entry per non-blank line.
    """
    if not isinstance(entry, dict):
        return []
    kind = entry.get("type")
    if kind in ("multi_static_select", "checkboxes"):
        selected = entry.get("selected_options")
        if not isinstance(selected, list):
            return []
        return [opt["value"] for opt in selected if isinstance(opt, dict) and isinstance(opt.get("value"), str)]
    if kind == "plain_text_input":
        value = entry.get("value")
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
    return []


def _coerce(name: str, spec: dict[str, Any], raw: str) -> Any:
    """Map a submitted string to the schema's JSON type.

    Slack's inputs constrain the text already; a value that still fails to coerce raises loudly.
    """
    ptype = spec.get("type")
    if ptype == "boolean":
        if raw == "true":
            return True
        if raw == "false":
            return False
        raise FormSchemaError(f"form field {name!r} boolean value not recognized: {raw!r}")
    if ptype == "integer":
        try:
            return int(raw)
        except ValueError as exc:
            raise FormSchemaError(f"form field {name!r} is not an integer: {raw!r}") from exc
    if ptype == "number":
        try:
            parsed = float(raw)
        except ValueError as exc:
            raise FormSchemaError(f"form field {name!r} is not a number: {raw!r}") from exc
        # inf/nan pass jsonschema's ``type: number`` and pydantic stores them as
        # null — a silent value loss. Decline the coercion so the raw string
        # travels to the callback door and its schema validation rejects it there,
        # the same recovery a non-numeric entry already takes.
        if not math.isfinite(parsed):
            return raw
        return parsed
    return raw


def extract_answer(schema: dict[str, Any], state_values: dict[str, Any]) -> dict[str, Any]:
    """The answer dict from a ``view_submission`` state.

    Each present, non-empty field coerced to its schema type (an array field returns a list,
    an unfilled optional field is omitted). A field whose block is absent from the state — a
    field hidden by its conditional predicate before submit — is omitted; the answer facet
    drops any hidden field left in the answer besides.
    """
    answer: dict[str, Any] = {}
    for name, spec in _properties(schema).items():
        field_name = str(name)
        block = state_values.get(field_name)
        if not isinstance(block, dict):
            continue
        entry = block.get(FIELD_ACTION_ID)
        spec_dict = spec if isinstance(spec, dict) else {}
        if spec_dict.get("type") == "array":
            values = _array_value(entry)
            if values:
                answer[field_name] = values
            continue
        raw = _raw_value(entry)
        if raw is None:
            continue
        answer[field_name] = _coerce(field_name, spec_dict, raw)
    return answer
