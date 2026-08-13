"""Schema → Block Kit mapping for ``answer_format == "form"`` questions.

Three shapes are built here, all from the same JSON answer schema (a top-level
``{"type": "object", "properties": {...}, "required": [...]}``):

* the outbound message blocks — a section carrying the question and one button
  (:data:`FORM_OPEN_ACTION_ID`) whose ``value`` is the interaction id;
* the modal view (:data:`FORM_SUBMIT_CALLBACK_ID`) opened on the button click,
  one input block per property;
* the answer dict extracted and coerced from a ``view_submission`` state.

Supported property subset (identical to the callback door's own form renderer, so
a modal answer validates there): ``string`` → ``plain_text_input``,
``string``+``enum`` → ``static_select``, ``boolean`` → ``radio_buttons`` (Yes/No →
``true``/``false``), ``integer``/``number`` → ``number_input``. Anything else, or a
value past a Slack cap, raises :class:`FormSchemaError` naming the property — never
a silently dropped or truncated field.
"""

from __future__ import annotations

import math
from typing import Any

# The message button and the modal it opens.
FORM_OPEN_ACTION_ID = "tai42_form_open"
FORM_SUBMIT_CALLBACK_ID = "tai42_form_submit"
# One fixed action_id per input block: block_id is the field name, so the pair
# ``state.values[<field name>][FIELD_ACTION_ID]`` reads a field's value directly.
FIELD_ACTION_ID = "tai42_form_field"
_BUTTON_LABEL = "Fill form"
_MODAL_TITLE = "Please respond"
_SUBMIT_LABEL = "Submit"
_CLOSE_LABEL = "Cancel"
_YES_NO_OPTIONS = (("Yes", "true"), ("No", "false"))

# Slack Block Kit caps: exceeding one is a loud error, never a truncation.
_MAX_MODAL_BLOCKS = 100
_MAX_LABEL_LEN = 2000
_MAX_SECTION_TEXT_LEN = 3000
_MAX_OPTION_TEXT_LEN = 75
_MAX_STATIC_SELECT_OPTIONS = 100
_MAX_BUTTON_VALUE_LEN = 2000


class FormSchemaError(ValueError):
    """A form schema (or a submitted value) cannot be mapped to Block Kit."""


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
    """The first property name — the fallback block a door-side error is pinned under
    when the door names no locatable field."""
    return next(iter(_properties(schema)))


def is_declared_field(schema: dict[str, Any], name: str) -> bool:
    """Whether ``name`` is a declared property of the schema — i.e. a real input
    block_id (block_id == field name) a door-side error can be pinned under."""
    return name in _properties(schema)


def _static_select(name: str, enum: Any) -> dict[str, Any]:
    if not isinstance(enum, list) or not enum:
        raise FormSchemaError(f"form schema property {name!r} enum must be a non-empty list")
    if len(enum) > _MAX_STATIC_SELECT_OPTIONS:
        raise FormSchemaError(f"form schema property {name!r} enum exceeds {_MAX_STATIC_SELECT_OPTIONS} options")
    options: list[dict[str, Any]] = []
    for choice in enum:
        text = str(choice)
        if len(text) > _MAX_OPTION_TEXT_LEN:
            raise FormSchemaError(
                f"form schema property {name!r} option {text!r} exceeds {_MAX_OPTION_TEXT_LEN} characters"
            )
        options.append({"text": {"type": "plain_text", "text": text}, "value": text})
    return {"type": "static_select", "action_id": FIELD_ACTION_ID, "options": options}


def _radio_buttons() -> dict[str, Any]:
    return {
        "type": "radio_buttons",
        "action_id": FIELD_ACTION_ID,
        "options": [
            {"text": {"type": "plain_text", "text": label}, "value": value} for label, value in _YES_NO_OPTIONS
        ],
    }


def _element(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    ptype = spec.get("type")
    if ptype == "string":
        enum = spec.get("enum")
        if enum is not None:
            return _static_select(name, enum)
        return {"type": "plain_text_input", "action_id": FIELD_ACTION_ID}
    if ptype == "boolean":
        return _radio_buttons()
    if ptype in ("integer", "number"):
        return {"type": "number_input", "action_id": FIELD_ACTION_ID, "is_decimal_allowed": ptype == "number"}
    raise FormSchemaError(f"form schema property {name!r} has unsupported type {ptype!r}")


def _input_block(name: str, spec: Any, is_required: bool) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise FormSchemaError(f"form schema property {name!r} must be an object")
    label = str(spec.get("title") or name)
    if len(label) > _MAX_LABEL_LEN:
        raise FormSchemaError(f"form schema property {name!r} label exceeds {_MAX_LABEL_LEN} characters")
    block: dict[str, Any] = {
        "type": "input",
        "block_id": name,
        "label": {"type": "plain_text", "text": label},
        "element": _element(name, spec),
    }
    if not is_required:
        # Slack input blocks are required unless flagged optional.
        block["optional"] = True
    return block


def _question_section(question: str) -> dict[str, Any]:
    if len(question) > _MAX_SECTION_TEXT_LEN:
        raise FormSchemaError(f"form question exceeds {_MAX_SECTION_TEXT_LEN} characters")
    return {"type": "section", "text": {"type": "plain_text", "text": question}}


def build_modal_blocks(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """One input block per property; raises :class:`FormSchemaError` on any
    property this mapping cannot express."""
    required = _required_set(schema)
    return [_input_block(str(name), spec, str(name) in required) for name, spec in _properties(schema).items()]


def build_modal_view(interaction_id: str, question: str, schema: dict[str, Any]) -> dict[str, Any]:
    """The ``views.open`` modal: the question as a section, then the input blocks.
    ``private_metadata`` carries the interaction id back on ``view_submission``."""
    blocks = [_question_section(question), *build_modal_blocks(schema)]
    if len(blocks) > _MAX_MODAL_BLOCKS:
        raise FormSchemaError(f"form modal exceeds {_MAX_MODAL_BLOCKS} blocks")
    return {
        "type": "modal",
        "callback_id": FORM_SUBMIT_CALLBACK_ID,
        "private_metadata": interaction_id,
        "title": {"type": "plain_text", "text": _MODAL_TITLE},
        "submit": {"type": "plain_text", "text": _SUBMIT_LABEL},
        "close": {"type": "plain_text", "text": _CLOSE_LABEL},
        "blocks": blocks,
    }


def build_message_blocks(question: str, interaction_id: str) -> list[dict[str, Any]]:
    """The ``chat.postMessage`` blocks: the question section plus a single button
    whose ``value`` is the interaction id (read back on ``block_actions``)."""
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
    """The submitted string of one field, or ``None`` when it was left empty."""
    if not isinstance(entry, dict):
        return None
    kind = entry.get("type")
    if kind in ("plain_text_input", "number_input"):
        value = entry.get("value")
        return value if isinstance(value, str) and value != "" else None
    if kind in ("static_select", "radio_buttons"):
        selected = entry.get("selected_option")
        if isinstance(selected, dict):
            value = selected.get("value")
            return value if isinstance(value, str) else None
        return None
    return None


def _coerce(name: str, spec: dict[str, Any], raw: str) -> Any:
    """Map a submitted string to the schema's JSON type. Slack's inputs constrain
    the text already; a value that still fails to coerce raises loudly."""
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
    """The answer dict from a ``view_submission`` state: each present, non-empty
    field coerced to its schema type (an unfilled optional field is omitted)."""
    answer: dict[str, Any] = {}
    for name, spec in _properties(schema).items():
        field_name = str(name)
        block = state_values.get(field_name)
        if not isinstance(block, dict):
            continue
        raw = _raw_value(block.get(FIELD_ACTION_ID))
        if raw is None:
            continue
        answer[field_name] = _coerce(field_name, spec if isinstance(spec, dict) else {}, raw)
    return answer
