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
a silently dropped or truncated field. A schema/cap violation is a permanent input
refusal (the medium cannot render it BY NATURE), so :class:`FormSchemaError` is a
:class:`~tai42_contract.channels.ChannelInputError`, never a retryable delivery
failure.

Per-send enrichment rides the same mapping: a ``values`` map prefills each named
property's control (``initial_value`` for a text/number input, ``initial_option``
for a select or the Yes/No radio); an ``options`` map supplies a per-send choice
list for a string property that BUILDS its ``static_select`` (its labels shown, its
values submitted), replacing the schema ``enum`` for this one send — per-send
options on a non-string property are refused, naming it. ``pages`` render as titled
``header`` sections within ONE modal: Slack modals have NO native multi-step (a view
is one surface with a single submit), so the steps become in-order titled groups of
the same input blocks; the submitted answer is the union of every field, exactly as
an unpaged modal. A per-send value not among its field's options is refused, naming
it — never silently dropped.
"""

from __future__ import annotations

import math
from typing import Any

from tai42_contract.channels import ChannelInputError

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
# A ``header`` block's plain_text cap — the surface a form page's title renders on.
_MAX_HEADER_LEN = 150


class FormSchemaError(ChannelInputError):
    """A form schema (or a submitted value) cannot be mapped to Block Kit — a
    permanent refusal, never a retryable delivery failure."""


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


def _option_block(name: str, label: str, value: str) -> dict[str, Any]:
    """One ``static_select`` option: the ``label`` shown, the ``value`` submitted."""
    if len(label) > _MAX_OPTION_TEXT_LEN:
        raise FormSchemaError(
            f"form schema property {name!r} option {label!r} exceeds {_MAX_OPTION_TEXT_LEN} characters"
        )
    return {"text": {"type": "plain_text", "text": label}, "value": value}


def _static_select(name: str, enum: Any) -> dict[str, Any]:
    if not isinstance(enum, list) or not enum:
        raise FormSchemaError(f"form schema property {name!r} enum must be a non-empty list")
    if len(enum) > _MAX_STATIC_SELECT_OPTIONS:
        raise FormSchemaError(f"form schema property {name!r} enum exceeds {_MAX_STATIC_SELECT_OPTIONS} options")
    # A schema enum shows and submits the same string — the label IS the value.
    options = [_option_block(name, str(choice), str(choice)) for choice in enum]
    return {"type": "static_select", "action_id": FIELD_ACTION_ID, "options": options}


def _static_select_from_options(name: str, choices: list[dict[str, Any]]) -> dict[str, Any]:
    """A ``static_select`` built from a per-send option list — each ``{"value", "label"?}``
    becomes an option whose label (falling back to the value) is shown and whose value is
    submitted. An empty list or one past the option cap is refused, naming the property."""
    if not choices:
        raise FormSchemaError(f"form schema property {name!r} per-send options must be a non-empty list")
    if len(choices) > _MAX_STATIC_SELECT_OPTIONS:
        raise FormSchemaError(f"form schema property {name!r} per-send options exceed {_MAX_STATIC_SELECT_OPTIONS}")
    options: list[dict[str, Any]] = []
    for choice in choices:
        value = choice["value"]
        label = choice.get("label") or value
        options.append(_option_block(name, label, value))
    return {"type": "static_select", "action_id": FIELD_ACTION_ID, "options": options}


def _find_option(options: list[dict[str, Any]], value: str) -> dict[str, Any] | None:
    """The option whose ``value`` matches, or ``None``."""
    return next((option for option in options if option["value"] == value), None)


def _apply_initial(name: str, element: dict[str, Any], value: Any) -> None:
    """Prefill one control from a per-send value: ``initial_value`` for a text/number
    input, ``initial_option`` for a select or the Yes/No radio. A select value that is
    not among the control's options — or a boolean value that is not a bool — is a
    caller bug, refused naming the field rather than silently dropped."""
    etype = element["type"]
    if etype in ("plain_text_input", "number_input"):
        element["initial_value"] = value if isinstance(value, str) else str(value)
        return
    if etype == "static_select":
        match = _find_option(element["options"], str(value))
        if match is None:
            raise FormSchemaError(f"form field {name!r} prefilled value {value!r} is not among its options")
        element["initial_option"] = match
        return
    # radio_buttons (boolean): the true/false option keyed by the bool.
    if not isinstance(value, bool):
        raise FormSchemaError(f"form field {name!r} prefilled boolean value must be true or false, got {value!r}")
    match = _find_option(element["options"], "true" if value else "false")
    if match is not None:
        element["initial_option"] = match


def _radio_buttons() -> dict[str, Any]:
    return {
        "type": "radio_buttons",
        "action_id": FIELD_ACTION_ID,
        "options": [
            {"text": {"type": "plain_text", "text": label}, "value": value} for label, value in _YES_NO_OPTIONS
        ],
    }


def _element(name: str, spec: dict[str, Any], per_send_options: list[dict[str, Any]] | None) -> dict[str, Any]:
    ptype = spec.get("type")
    if per_send_options is not None:
        # A per-send option list BUILDS the select, replacing the schema enum for this send.
        # It rides a string property only; on anything else it is refused loudly (never
        # rendered as a mismatched control).
        if ptype != "string":
            raise FormSchemaError(
                f"form schema property {name!r} carries per-send options but its type is {ptype!r}, not string"
            )
        return _static_select_from_options(name, per_send_options)
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


def _input_block(
    name: str,
    spec: Any,
    is_required: bool,
    value: Any = None,
    per_send_options: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise FormSchemaError(f"form schema property {name!r} must be an object")
    label = str(spec.get("title") or name)
    if len(label) > _MAX_LABEL_LEN:
        raise FormSchemaError(f"form schema property {name!r} label exceeds {_MAX_LABEL_LEN} characters")
    element = _element(name, spec, per_send_options)
    if value is not None:
        _apply_initial(name, element, value)
    block: dict[str, Any] = {
        "type": "input",
        "block_id": name,
        "label": {"type": "plain_text", "text": label},
        "element": element,
    }
    if not is_required:
        # Slack input blocks are required unless flagged optional.
        block["optional"] = True
    return block


def _question_section(question: str) -> dict[str, Any]:
    if len(question) > _MAX_SECTION_TEXT_LEN:
        raise FormSchemaError(f"form question exceeds {_MAX_SECTION_TEXT_LEN} characters")
    return {"type": "section", "text": {"type": "plain_text", "text": question}}


def _page_header(title: str) -> dict[str, Any]:
    """One form page's title as a Block Kit ``header`` — a bold titled group, Slack's
    stand-in for a step (a modal has no native multi-step)."""
    if len(title) > _MAX_HEADER_LEN:
        raise FormSchemaError(f"form page title {title!r} exceeds {_MAX_HEADER_LEN} characters")
    return {"type": "header", "text": {"type": "plain_text", "text": title}}


def build_modal_blocks(
    schema: dict[str, Any],
    values: dict[str, Any] | None = None,
    options: dict[str, list[dict[str, Any]]] | None = None,
    pages: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One input block per property, each prefilled from ``values`` and — for a string
    property named in ``options`` — built as a select from that per-send choice list.
    With ``pages`` the blocks are grouped under a ``header`` per page (in page order);
    without them the properties render in schema order. Raises :class:`FormSchemaError`
    on any property this mapping cannot express or any per-send extra it cannot map."""
    values = values or {}
    options = options or {}
    required = _required_set(schema)
    properties = _properties(schema)
    if pages is not None:
        blocks: list[dict[str, Any]] = []
        for page in pages:
            blocks.append(_page_header(str(page["title"])))
            for field in page["fields"]:
                name = str(field)
                spec = properties.get(name)
                if spec is None:
                    raise FormSchemaError(f"form page names unknown property {name!r}")
                blocks.append(_input_block(name, spec, name in required, values.get(name), options.get(name)))
        return blocks
    return [
        _input_block(str(name), spec, str(name) in required, values.get(str(name)), options.get(str(name)))
        for name, spec in properties.items()
    ]


def build_modal_view(
    interaction_id: str,
    question: str,
    schema: dict[str, Any],
    values: dict[str, Any] | None = None,
    options: dict[str, list[dict[str, Any]]] | None = None,
    pages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The ``views.open`` modal: the question as a section, then the input blocks
    (prefilled, per-send selects, and page headers as applicable).
    ``private_metadata`` carries the interaction id back on ``view_submission``."""
    blocks = [_question_section(question), *build_modal_blocks(schema, values, options, pages)]
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


def validate_form_schema(schema: dict[str, Any], question: str) -> None:
    """Enforce the ask-time-knowable Block Kit caps at ask-time; raise
    :class:`FormSchemaError` naming the offending property/limit on any violation.

    Covers every limit knowable before delivery: the question-text section cap,
    the supported property subset, the per-label cap, the static-select option
    count and per-option text caps, and the modal's 100-block cap (one question
    section plus one input block per property). The button-value cap depends on
    the per-send interaction id, not the schema or question, so it stays at
    delivery."""
    if len(question) > _MAX_SECTION_TEXT_LEN:
        raise FormSchemaError(f"form question exceeds {_MAX_SECTION_TEXT_LEN} characters")
    blocks = build_modal_blocks(schema)
    # +1 for the question section the modal view prepends before the input blocks.
    if len(blocks) + 1 > _MAX_MODAL_BLOCKS:
        raise FormSchemaError(f"form modal exceeds {_MAX_MODAL_BLOCKS} blocks")


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
