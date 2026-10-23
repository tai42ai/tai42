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
a modal answer validates there): ``string`` → ``plain_text_input``, except a
``string`` whose ``inputMode`` is ``email`` (or whose ``format`` is ``email``) → ``email_text_input`` or ``url`` →
``url_text_input`` (Slack's own text elements, which carry their built-in check),
while ``numeric``/``decimal``/``tel`` have no Slack keyboard hint and degrade to a
``plain_text_input`` (the value always arrives exactly as typed); ``string``
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
(Yes/No → ``true``/``false``); ``integer``/``number`` → ``number_input`` (its inclusive
``minimum``/``maximum`` drawn as ``min_value``/``max_value``). Anything
else, or a value past a Slack cap, raises :class:`FormSchemaError` naming the
property — never a silently dropped or truncated field. A property schema's
``description`` renders as the input block's ``hint`` (the field's second line); a
blank/whitespace-only ``description`` is absent.

Per-send enrichment rides the same mapping: a ``values`` map prefills each named
property's control; an ``options`` map supplies a per-send choice list (its labels
shown, its values submitted, each option's optional ``description`` drawn as the
option object's second line) replacing a string/array property's ``enum`` for this
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

import json
import math
from typing import Any, NamedTuple

from tai42_contract.interactions import FORM_REACTION_REASON_MAX_CHARS
from tai42_kit.interactions import RADIO_OPTION_THRESHOLD, drawing_order, hidden_fields

from tai42_channel_slack.form_blocks import (
    FIELD_ACTION_ID,
    FormSchemaError,
    _apply_initial,
    _boolean_words,
    _element,
)

__all__ = [
    "FIELD_ACTION_ID",
    "FORM_OPEN_ACTION_ID",
    "FORM_SUBMIT_CALLBACK_ID",
    "RADIO_OPTION_THRESHOLD",
    "FormSchemaError",
    "ModalMetadata",
    "build_message_blocks",
    "build_modal_blocks",
    "build_modal_view",
    "check_update_failed_line",
    "decode_private_metadata",
    "extract_answer",
    "first_field_name",
    "is_declared_field",
    "validate_form_schema",
]

# The message button and the modal it opens.
FORM_OPEN_ACTION_ID = "tai42_form_open"
FORM_SUBMIT_CALLBACK_ID = "tai42_form_submit"
_BUTTON_LABEL = "Fill form"
_MODAL_TITLE = "Please respond"
_SUBMIT_LABEL = "Submit"
_CLOSE_LABEL = "Cancel"

# Slack Block Kit caps: exceeding one is a loud error, never a truncation.
_MAX_MODAL_BLOCKS = 100
_MAX_LABEL_LEN = 2000
# An ``input`` block's optional ``hint`` plain_text cap — the surface a field's second line
# (the property schema's ``description``) renders on.
_MAX_HINT_LEN = 2000
_MAX_SECTION_TEXT_LEN = 3000
_MAX_BUTTON_VALUE_LEN = 2000
# A ``header`` block's plain_text cap — the surface a form page's title (and a
# display heading) renders on.
_MAX_HEADER_LEN = 150
# The ``private_metadata`` string cap Slack enforces on a view.
_MAX_PRIVATE_METADATA_LEN = 3000
# A modal view's ``title``/``submit``/``close`` ``plain_text`` cap.
_MAX_MODAL_TEXT_LEN = 24
# A message ``button`` element's ``text`` cap — the surface the open button draws on.
_MAX_BUTTON_TEXT_LEN = 75

# A triggering text input dispatches its ``block_actions`` when the human presses
# enter (a select/radio/checkbox/datepicker dispatches on selection with no config).
_TEXT_DISPATCH_CONFIG = {"trigger_actions_on": ["on_enter_pressed"]}

# The line a review page shows before any value has been entered.
_REVIEW_EMPTY_TEXT = "No answers to review yet."


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


def _input_block(
    name: str,
    spec: Any,
    is_required: bool,
    value: Any = None,
    per_send_options: list[dict[str, Any]] | None = None,
    *,
    dispatch: bool = False,
    choice_label: str | None = None,
    boolean_words: tuple[str, str] | None = None,
) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise FormSchemaError(f"form schema property {name!r} must be an object")
    label = str(spec.get("title") or name)
    if len(label) > _MAX_LABEL_LEN:
        raise FormSchemaError(f"form schema property {name!r} label exceeds {_MAX_LABEL_LEN} characters")
    element = _element(name, spec, per_send_options, choice_label, boolean_words or _boolean_words(None))
    if value is not None:
        _apply_initial(name, element, value)
    if dispatch and element["type"] in ("plain_text_input", "number_input", "email_text_input", "url_text_input"):
        # A text/number/email/url input dispatches on enter; a selection control dispatches on its own.
        element["dispatch_action_config"] = _TEXT_DISPATCH_CONFIG
    block: dict[str, Any] = {
        "type": "input",
        "block_id": name,
        "label": {"type": "plain_text", "text": label},
        "element": element,
    }
    hint = spec.get("description")
    if isinstance(hint, str) and hint.strip():
        # The field's second line: the property schema's ``description`` drawn as the input
        # block's ``hint``. A blank/whitespace-only description is absent (drawn as nothing),
        # the same rule the title takes; its content carries answer-relevant data, so a value
        # past Slack's hint cap is refused naming the property, never truncated or dropped.
        if len(hint) > _MAX_HINT_LEN:
            raise FormSchemaError(f"form schema property {name!r} hint exceeds {_MAX_HINT_LEN} characters")
        block["hint"] = {"type": "plain_text", "text": hint}
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


def _readback_value(value: Any, words: tuple[str, str]) -> str:
    # One review-line rendering of a submitted value: the field's true/false word for a bool,
    # comma-joined for a list, the string otherwise.
    if isinstance(value, bool):
        return words[0] if value else words[1]
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _readback_section(
    properties: dict[str, Any],
    values: dict[str, Any],
    hidden: set[str],
    review_empty: str | None = None,
    field_wording: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # A review page's generic readback of the values entered so far: one ``*label*: value``
    # line per visible, filled field. A field hidden by its predicate is omitted (it is not
    # part of the answer); when nothing is filled yet the empty line is shown — the caller's
    # ``review_empty`` when set, else the platform default. A boolean reads in its field entry's
    # ``true_label`` / ``false_label``, else "Yes" / "No". The lines come in drawing order, each
    # ``timeOf`` time right after its date.
    field_wording = field_wording or {}
    lines: list[str] = []
    for name in drawing_order(list(properties), properties):
        spec = properties[name]
        if name in hidden or name not in values:
            continue
        label = str((spec.get("title") if isinstance(spec, dict) else None) or name)
        words = _boolean_words(field_wording.get(name))
        lines.append(f"*{label}*: {_readback_value(values[name], words)}")
    text = "\n".join(lines) if lines else (review_empty or _REVIEW_EMPTY_TEXT)
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
    field_wording: dict[str, Any],
) -> list[dict[str, Any]]:
    # One field's blocks: its input block (prefilled, with a dispatch flag when it is a
    # reaction/conditional trigger), plus a context block for any reaction error on it. A
    # field hidden by its predicate contributes nothing; an unknown field is refused. A per-field
    # ``choice_label`` draws the field's select placeholder (a non-select control ignores it).
    spec = properties.get(name)
    if spec is None:
        raise FormSchemaError(f"form page names unknown property {name!r}")
    if name in hidden:
        return []
    prefill = values.get(name) if values is not None else None
    entry = field_wording.get(name)
    choice_label = entry.get("choice_label") if isinstance(entry, dict) else None
    block = _input_block(
        name,
        spec,
        name in required,
        prefill,
        options.get(name),
        dispatch=name in triggers,
        choice_label=choice_label,
        boolean_words=_boolean_words(entry),
    )
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
    review_empty: str | None,
    field_wording: dict[str, Any],
) -> list[dict[str, Any]]:
    # One page: its title header, its ordered display blocks, then either a review readback
    # (a review page) or its input fields (an input page) in drawing order, each ``timeOf`` time
    # right after its date.
    blocks: list[dict[str, Any]] = [_page_header(str(page["title"]))]
    for display in page.get("display") or []:
        rendered = _display_block(display, display_values)
        if rendered is not None:
            blocks.append(rendered)
    if page.get("kind") == "review":
        blocks.append(_readback_section(properties, values or {}, hidden, review_empty, field_wording))
        return blocks
    for field in drawing_order([str(field) for field in page["fields"]], properties):
        blocks.extend(
            _field_group(field, properties, required, values, options, hidden, triggers, field_errors, field_wording)
        )
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
    review_empty: str | None = None,
    field_wording: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The input/display blocks of a form modal.

    Each property renders one prefilled input block (per-send ``options`` build its choice
    list). With ``pages`` the blocks are grouped under a ``header`` per page, interleaved with
    the page's ordered ``display`` blocks, and a ``review`` page shows a generic readback of
    the entered values; without ``pages`` the properties render in schema order (no display).
    Either way the fields draw in drawing order: each ``timeOf`` time right after its date.
    A property's ``visibleWhen`` predicate, evaluated here against ``values`` (``None`` =
    evaluate against nothing, so every field shows — the cap upper bound and the show-all
    base), hides a field; a reaction/conditional trigger field carries ``dispatch_action``; a
    reaction's ``display_values`` fill display slots and its ``field_errors`` render as context
    lines. ``review_empty`` is the caller's line a review page shows before anything is filled;
    ``field_wording`` carries per-field words (``{name: {"choice_label": ..., "true_label": ...,
    "false_label": ...}}``): the select placeholder of each named field and a boolean's two words
    on its radio options and its review line. Raises :class:`FormSchemaError` on any property this mapping
    cannot express or any per-send extra it cannot map.
    """
    options = options or {}
    display_values = display_values or {}
    field_errors = field_errors or {}
    field_wording = field_wording or {}
    required = _required_set(schema)
    properties = _properties(schema)
    # The platform's own visibility rule, so the modal shows and hides exactly what the answer
    # check enforces at submit, without ever calling the consumer.
    hidden = hidden_fields(properties, values) if values is not None else set()
    triggers = _trigger_fields(properties, reactions)
    if pages is not None:
        blocks: list[dict[str, Any]] = []
        for page in pages:
            blocks.extend(
                _page_blocks(
                    page,
                    properties,
                    required,
                    values,
                    options,
                    hidden,
                    triggers,
                    field_errors,
                    display_values,
                    review_empty,
                    field_wording,
                )
            )
        return blocks
    blocks = []
    for name in drawing_order([str(name) for name in properties], properties):
        blocks.extend(
            _field_group(name, properties, required, values, options, hidden, triggers, field_errors, field_wording)
        )
    return blocks


def _encode_private_metadata(
    interaction_id: str,
    metadata_options: dict[str, list[dict[str, Any]]] | None = None,
    metadata_display: dict[str, Any] | None = None,
    unavailable: str | None = None,
) -> str:
    """The view's ``private_metadata`` string: the interaction id plus the accumulated reaction state.

    The interaction id is the correlation key every ``view_submission``/``block_actions``
    carries back; the reaction-supplied option lists and display slot values ride alongside it
    so they survive a re-render (Slack echoes only entered VALUES in the view state).
    ``unavailable`` is the caller's ``status.unavailable`` line, written as an ``unavailable`` key
    only when set, so a submit that finds the form record gone still draws the caller's words.
    Raises :class:`FormSchemaError` when the encoded state exceeds Slack's ``private_metadata``
    cap — never a silent truncation.
    """
    payload: dict[str, Any] = {"id": interaction_id}
    if metadata_options:
        payload["options"] = metadata_options
    if metadata_display:
        payload["display"] = metadata_display
    if unavailable is not None:
        payload["unavailable"] = unavailable
    encoded = json.dumps(payload, separators=(",", ":"))
    if len(encoded) > _MAX_PRIVATE_METADATA_LEN:
        raise FormSchemaError(f"form modal private_metadata exceeds {_MAX_PRIVATE_METADATA_LEN} characters")
    return encoded


class ModalMetadata(NamedTuple):
    """What a view's ``private_metadata`` carries back on the next interaction.

    ``interaction_id`` is the correlation key; ``options`` / ``display`` are the accumulated
    reaction state; ``unavailable`` is the caller's ``status.unavailable`` line, or ``None``.
    """

    interaction_id: str
    options: dict[str, Any]
    display: dict[str, Any]
    unavailable: str | None


def decode_private_metadata(raw: Any) -> ModalMetadata:
    """The :class:`ModalMetadata` a view's ``private_metadata`` carries.

    Raises :class:`ValueError` when the metadata is missing or malformed (a present
    ``unavailable`` that is not a non-blank string included) — a view the plugin did not stamp is
    a loud bug, never a silent default.
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
    unavailable = data.get("unavailable")
    if unavailable is not None and (not isinstance(unavailable, str) or not unavailable.strip()):
        raise ValueError("form view private_metadata unavailable must be a non-blank string")
    return ModalMetadata(interaction_id, options, display, unavailable)


def _modal_text(role: str, label: str | None, default: str) -> dict[str, Any]:
    """One modal ``plain_text`` chrome field (title/submit/close): the caller's label else the default.

    A caller label over Slack's :data:`_MAX_MODAL_TEXT_LEN`-character modal-text cap is a loud
    :class:`FormSchemaError` naming the role — never a silent truncation of the caller's words.
    """
    text = label if label is not None else default
    if len(text) > _MAX_MODAL_TEXT_LEN:
        raise FormSchemaError(f"form modal {role} {text!r} exceeds {_MAX_MODAL_TEXT_LEN} characters")
    return {"type": "plain_text", "text": text}


def _submit_label(pages: list[dict[str, Any]] | None) -> str | None:
    """The form's terminal submit label — the LAST page's ``button_label`` — or ``None`` for the default.

    Slack renders every page of a form in ONE modal with ONE ``submit``, so the submit takes the
    LAST page's label (the form's terminal action); a non-terminal page's label is not drawn —
    Slack has no per-page button (the documented degrade). A form with no ``pages`` has no page
    label and shows the default word.
    """
    if not pages:
        return None
    last = pages[-1]
    label = last.get("button_label") if isinstance(last, dict) else None
    return label if isinstance(label, str) else None


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
    form_wording: dict[str, Any] | None = None,
    status_unavailable: str | None = None,
) -> dict[str, Any]:
    """The ``views.open``/``views.update`` modal: the question as a section, then the blocks.

    The input/display/review blocks are built with the per-send prefill, option lists, display
    slots and any reaction errors; ``private_metadata`` carries the interaction id plus the
    accumulated reaction state (``metadata_options``/``metadata_display``) back on the next
    interaction. ``form_wording`` (the ``wording.form`` chapter) carries the caller's words, the
    platform's as the default: ``title`` is ``form_wording["title"]`` else "Please respond";
    ``submit`` is the LAST page's ``button_label``, else — for a page-less form — the caller's
    ``form_wording["submit"]``, else "Submit"; ``close`` is ``form_wording["cancel"]`` else
    "Cancel". Each is capped at Slack's 24-character modal-text limit, refused loudly over it
    (:class:`FormSchemaError`). ``form_wording["review_empty"]`` is the review step's empty line
    and ``form_wording["fields"]`` the per-field words (the select placeholders and a boolean's
    ``true_label`` / ``false_label``), threaded into the blocks. ``status_unavailable`` (the
    caller's ``status.unavailable``) rides ``private_metadata``, so the submit door draws it even
    when the form record is gone.
    """
    form_wording = form_wording or {}
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
            review_empty=form_wording.get("review_empty"),
            field_wording=form_wording.get("fields") or {},
        ),
    ]
    if len(blocks) > _MAX_MODAL_BLOCKS:
        raise FormSchemaError(f"form modal exceeds {_MAX_MODAL_BLOCKS} blocks")
    return {
        "type": "modal",
        "callback_id": FORM_SUBMIT_CALLBACK_ID,
        "private_metadata": _encode_private_metadata(
            interaction_id, metadata_options, metadata_display, status_unavailable
        ),
        "title": _modal_text("title", form_wording.get("title"), _MODAL_TITLE),
        "submit": _modal_text("submit", _submit_label(pages) or form_wording.get("submit"), _SUBMIT_LABEL),
        "close": _modal_text("close", form_wording.get("cancel"), _CLOSE_LABEL),
        "blocks": blocks,
    }


def check_update_failed_line(template: str | None) -> None:
    """Refuse a caller ``form.update_failed`` line the re-render banner cannot draw.

    The banner is a ``section`` mrkdwn text, capped at :data:`_MAX_SECTION_TEXT_LEN` characters.
    Each ``{reason}`` token takes the reaction door's reason, bounded at
    :data:`~tai42_contract.interactions.FORM_REACTION_REASON_MAX_CHARS`, so the line's worst case
    (its fixed text plus every token filled at the bound) is checked; over the cap is a loud
    :class:`FormSchemaError`, never a truncation.
    """
    if template is None:
        return
    reason_count = template.count("{reason}")
    worst_case = len(template.replace("{reason}", "")) + reason_count * FORM_REACTION_REASON_MAX_CHARS
    if worst_case > _MAX_SECTION_TEXT_LEN:
        raise FormSchemaError(
            f"wording.form.update_failed expands to up to {worst_case} characters once each of its "
            f"{reason_count} {{reason}} token(s) is filled (up to {FORM_REACTION_REASON_MAX_CHARS} characters "
            f"each); the form update banner is a section text Slack caps at {_MAX_SECTION_TEXT_LEN} characters"
        )


def validate_form_schema(schema: dict[str, Any], question: str) -> None:
    """Enforce the ask-time-knowable Block Kit caps at ask-time.

    Raises :class:`FormSchemaError` naming the offending property/limit on any violation.

    Covers every limit knowable before delivery: the question-text section cap, the supported
    property subset, the per-label cap, the field hint cap (the property schema's
    ``description`` second line), the option count and per-option text caps, and the modal's
    100-block cap. The per-send layout (pages/display), the per-send option descriptions and
    the button-value cap depend on the per-send interaction, not the schema or question, so
    they stay at delivery. Every field is counted shown (``values`` left unset), the modal's
    upper bound.
    """
    if len(question) > _MAX_SECTION_TEXT_LEN:
        raise FormSchemaError(f"form question exceeds {_MAX_SECTION_TEXT_LEN} characters")
    blocks = build_modal_blocks(schema)
    # +1 for the question section the modal view prepends before the input blocks.
    if len(blocks) + 1 > _MAX_MODAL_BLOCKS:
        raise FormSchemaError(f"form modal exceeds {_MAX_MODAL_BLOCKS} blocks")


def build_message_blocks(question: str, interaction_id: str, open_label: str | None = None) -> list[dict[str, Any]]:
    """The ``chat.postMessage`` blocks: the question section plus the open-form button.

    The button's ``value`` is the interaction id (read back on ``block_actions``); its ``text``
    is the caller's ``open_label`` else "Fill form", capped at Slack's
    :data:`_MAX_BUTTON_TEXT_LEN`-character button-text limit — a longer caller label is refused
    loudly (:class:`FormSchemaError`), never truncated.
    """
    if len(interaction_id) > _MAX_BUTTON_VALUE_LEN:
        raise FormSchemaError(f"interaction id exceeds the {_MAX_BUTTON_VALUE_LEN}-character button value cap")
    label = open_label if open_label is not None else _BUTTON_LABEL
    if len(label) > _MAX_BUTTON_TEXT_LEN:
        raise FormSchemaError(f"form open button label {label!r} exceeds {_MAX_BUTTON_TEXT_LEN} characters")
    return [
        _question_section(question),
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": FORM_OPEN_ACTION_ID,
                    "value": interaction_id,
                    "text": {"type": "plain_text", "text": label},
                }
            ],
        },
    ]


def _raw_value(entry: Any) -> str | None:
    """The submitted string of one scalar field, or ``None`` when it was left empty."""
    if not isinstance(entry, dict):
        return None
    kind = entry.get("type")
    if kind in ("plain_text_input", "number_input", "email_text_input", "url_text_input"):
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
    """Lift a submitted string to the JSON type its schema property declares.

    Slack delivers every scalar input as a string. ``"true"``/``"false"`` for a
    ``boolean``, an integer literal for an ``integer`` and a finite literal for a
    ``number`` become the typed value; any other string is returned as submitted,
    and the answer door rejects a value of the wrong type against the schema with a
    field-located reason the modal shows inline. A non-finite ``number`` literal
    stays a string on purpose: as a float it would pass ``type: number`` and
    serialize to null.
    """
    ptype = spec.get("type")
    if ptype == "boolean":
        if raw == "true":
            return True
        if raw == "false":
            return False
        return raw
    if ptype == "integer":
        try:
            return int(raw)
        except ValueError:
            return raw
    if ptype == "number":
        try:
            parsed = float(raw)
        except ValueError:
            return raw
        # A non-finite float passes ``type: number`` and serializes to null; the
        # string does not pass, so it is forwarded for the door to reject.
        if not math.isfinite(parsed):
            return raw
        return parsed
    return raw


def extract_answer(schema: dict[str, Any], state_values: dict[str, Any]) -> dict[str, Any]:
    """The answer dict from a ``view_submission`` state.

    Each present, non-empty field lifted to its schema type when its string denotes one, else
    forwarded as submitted (an array field returns a list, an unfilled optional field is
    omitted). A field whose block is absent from the state — a
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
