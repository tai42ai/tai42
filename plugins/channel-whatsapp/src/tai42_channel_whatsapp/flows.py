"""Answer-schema → WhatsApp Flow JSON mapping.

An ``ask`` form ask carries a JSON answer schema; this module renders it as a
publishable WhatsApp Flow the human fills in-chat. The supported subset is a
top-level ``{"type": "object", "properties": {...}, "required": [...]}`` whose
properties map one-to-one onto Flow field components:

* ``string``                    → ``TextInput`` (its ``inputMode`` selects the keyboard, below)
* ``string`` with ``enum``      → ``Dropdown`` (dynamic ``data-source`` items)
* ``string`` + ``format: date`` → single-mode ``CalendarPicker`` (sets/returns a ``YYYY-MM-DD`` string)
* ``string`` + ``format: time`` / ``date-time`` → ``TextInput`` carrying the platform's format regex as
  its ``pattern`` (the vendor has no time-of-day picker, and its ``CalendarPicker`` is a calendar,
  not a date-time control)
* ``string`` + ``format: email`` → ``TextInput`` with ``input-type: "email"``
* ``boolean``                   → ``OptIn``
* ``integer`` / ``number``      → ``TextInput`` with ``input-type: "number"`` (an integer also draws
  the whole-number ``pattern``)

A ``string`` field's ``inputMode`` hint selects the ``TextInput`` keyboard (the value still
arrives as the typed string, never coerced):

* ``numeric`` / ``decimal``     → ``input-type: "number"`` (the numeric keypad admits a decimal separator)
* ``tel``                       → ``input-type: "phone"``
* ``email``                     → ``input-type: "email"``
* ``url``                       → plain ``TextInput`` — Meta has no ``url`` input-type, so this
  degrades to the plain text field (the kind is honoured on surfaces that draw it)
* ``text`` / absent             → plain ``TextInput``

``format`` is part of the platform's form-schema subset (validated at the ask door); this
module only chooses the WhatsApp control for it. Every date draws a ``CalendarPicker`` (not the
``DatePicker``, whose reference defines no ``required``, ``helper-text`` or ``error-message``), so a
date's required flag is checked by the control. An enum or option-bearing string stays a
``Dropdown`` even with ``format: date``: an explicit choice list outranks a format hint.

A field's declared shape is checked inside the form (:mod:`tai42_channel_whatsapp.flows_shapes`):
Meta's controls check what they can natively (``required``, a ``pattern`` on the text/number
keyboard, the length/selection/calendar bounds), and a form carrying any other shape — or a
consumer reaction — is ENDPOINT-DRIVEN (:func:`form_needs_endpoint`): every footer fires a
``data_exchange`` the data endpoint answers with the platform's one form-shape check, keeping the
person on the screen with the failing field's ``error-message`` while a shape is broken.

Each property's ``title`` (else the property name) is the field label; the
``required`` list flags the components. Anything outside the subset — a nested
object, an array, a ``oneOf``/``anyOf``, an unknown type, a string ``enum`` that is
not a non-empty list of strings, or the reserved ``flow_token`` property name — is a
permanent input refusal (the medium cannot render it BY NATURE): it raises
``ChannelInputError`` naming the property and why, before any network work, and is
never retried.

``build_form_flow`` is the one builder. Each page becomes its own screen — screen
ids are letters-and-underscores only (``SCREEN_A`` the entry the send navigates to,
``SCREEN_B`` …, the last terminal), so the vendor's publish accepts them — and every
screen is a ``SingleColumnLayout`` holding its field components and its footer
DIRECTLY, with no ``Form`` wrapper. Every control carries its own ``init-value``
(``${data.<field>__init}``) and a choice field a dynamic ``data-source``
(``${data.<field>__ds}``), so a send injects the prefilled values and the per-send
option lists through ``flow_action_payload.data`` (built by :func:`build_flow_data`)
rather than re-publishing. Collected values thread forward across screens by each
step's navigate payload; the terminal screen completes with the flat UNION of every
field, so the inbound decode reads it exactly as an unpaged form. A footer reads a
control's just-filled value through the form-input reference ``${form.<field>}``
(Meta's reference for data the user entered on the screen, valid with or without a
``Form`` wrapper); an earlier screen's value rides forward as ``${data.<field>__val}``.

Meta accepts a component ``name``, a screen-``data`` key and a ``${data.…}`` /
``${form.…}`` reference only in the identifier grammar
``[A-Za-z_][A-Za-z0-9_]*``, while an answer schema may use ANY JSON property name.
So each schema property is first mapped to a unique identifier-safe COMPONENT NAME
(:func:`component_names`), and that component name — never the raw property name — is
the ``<field>`` every component ``name``, data key, reference and navigate-payload key
above is spelled with. The terminal completion payload is keyed differently: by each
property's human-readable LABEL (:func:`payload_labels` — the property ``title`` when a non-empty
string, else the property key), because WhatsApp shows those keys to the participant in the
submitted-form summary. Its VALUES stay the identifier-safe component references, and the
inbound ``nfm_reply`` decode maps each label back to the schema key before coercion. The
field label a control shows uses the same :func:`_field_label` rule, so the summary key
matches the label the participant filled, and either may carry any character.

A string property renders as a dynamic ``Dropdown`` when it carries a schema ``enum``
OR when the ask marks it option-bearing (``option_fields`` — the set of
``flow_action_payload.data.options`` keys); this matches the web and Slack channels,
which honor per-send options on ANY string property. Because that set decides which
Flow is published, it joins the publish key: a schema reused with a different
option-bearing set publishes its own Flow, while an unchanged triple reuses one. A
string property that is neither enum nor option-bearing stays a ``TextInput``. A
per-send option list on a NON-STRING property cannot be honored (only a string maps
to a dropdown) and is refused loudly, naming the field.

``build_form_flow`` returns the ``(flow_json, key)`` pair; ``key`` is a sha256 over
the ``(schema, pages, option_fields, flow_json)`` — the emitted Flow folded in, so a
change to the emitted shape re-keys the published-Flow cache — and keys that cache.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from tai42_contract.channels import ChannelInputError
from tai42_contract.interactions import FormReactionEventKind
from tai42_kit.interactions import RADIO_OPTION_THRESHOLD

from tai42_channel_whatsapp.flows_components import (
    _RESERVED_PROPERTY,
    _calendar_range_component,
    _display_component_list,
    _dynamic_component,
    _field_control,
    _field_data_decls,
    _forward_field_data,
    _range_value_type_decl,
    _value_type_decl,
    component_names,
    payload_labels,
)
from tai42_channel_whatsapp.flows_endpoint import (
    REACTION_PAGE_KEY,
    _attach_field_reaction,
    _reaction_sets,
    error_fields,
    event_payload,
    form_endpoint_driven,
    guard_endpoint_names,
    screen_data_refs,
    shaped_fields,
)
from tai42_channel_whatsapp.flows_resolve import _form_range_merges, _resolve_pages, slot_datanames
from tai42_channel_whatsapp.flows_second_line import data_source_rows, field_caption, range_captions
from tai42_channel_whatsapp.flows_send_data import build_flow_data
from tai42_channel_whatsapp.flows_shapes import ERROR_EXAMPLE
from tai42_channel_whatsapp.flows_visibility import emit_field_variants, form_ref, screen_field_variants

__all__ = [
    "FORM_ENTRY_SCREEN",
    "RADIO_OPTION_THRESHOLD",
    "SUCCESS_SCREEN",
    "build_flow_data",
    "build_form_flow",
    "component_names",
    "data_source_rows",
    "form_needs_endpoint",
    "payload_labels",
    "slot_datanames",
]

# Flow JSON version pinned to a Cloud-API-valid release (≥ 6.2, so a TextInput takes a
# ``pattern``). Bump this constant when a newer schema version is adopted.
_FLOW_JSON_VERSION = "7.0"

# The per-page screen id prefix. A screen id must be letters-and-underscores only
# (the vendor rejects a digit in an ``id`` at publish), so the page index's decimal
# digits are mapped onto letters — ``SCREEN_A`` is the entry screen the send
# navigates to, then ``SCREEN_B`` … one screen per page, the last terminal.
_SCREEN_PREFIX = "SCREEN_"
# The digit→letter table: decimal digit ``d`` maps to the ``d``-th letter, so the
# index's decimal representation stays injective and the ids stay unique and
# unbounded (``SCREEN_J`` is 9, ``SCREEN_BA`` is 10).
_SCREEN_DIGIT_LETTERS = "ABCDEFGHIJ"
# Generic labels — a form ask carries no domain-specific chrome; each is the DEFAULT a page's
# ``button_label`` overrides in the sender's own words.
_FOOTER_LABEL = "Submit"
# The non-terminal step's footer — advances to the next screen.
_CONTINUE_LABEL = "Continue"
# Meta's ``Footer.label`` cap. A per-page ``button_label`` over this is refused loudly at build
# (never truncated), the same discipline the per-send caps take.
_FOOTER_LABEL_MAX_CHARS = 35

# The data-exchange API version an endpoint-driven Flow declares in its Flow JSON.
# A static (navigate-only) Flow carries none. Meta's current data-endpoint version.
_DATA_API_VERSION = "3.0"

# The screen a data-endpoint response names to COMPLETE an endpoint-driven Flow (Meta's reserved
# terminal screen); the completed answer rides ``extension_message_response.params``.
SUCCESS_SCREEN = "SUCCESS"


def _screen_id(index: int) -> str:
    """The screen id for a zero-based page index — letters-and-underscores only.

    The index's decimal digits are mapped onto :data:`_SCREEN_DIGIT_LETTERS`, so no
    id ever carries a digit (which the vendor rejects at publish) yet the ids stay
    unique and unbounded in page count.
    """
    return _SCREEN_PREFIX + "".join(_SCREEN_DIGIT_LETTERS[int(digit)] for digit in str(index))


# The entry screen a form send navigates to (index 0). The single source of truth
# for that id, imported by the send path so it can never drift from the builder.
FORM_ENTRY_SCREEN = _screen_id(0)


def _validate_object_schema(schema: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """The ``(properties, required)`` of a supported top-level object schema.

    Raises ``ChannelInputError`` naming what is outside the subset — the type is ``object``, ``properties``
    is a non-empty object, and ``required`` is a list of property names.
    """
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ChannelInputError(
            f"form schema must be a top-level object schema, got type={schema.get('type')!r}"
            if isinstance(schema, dict)
            else "form schema must be a JSON object"
        )
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise ChannelInputError("form schema must carry a non-empty 'properties' object")
    required_raw = schema.get("required", [])
    if not isinstance(required_raw, list) or not all(isinstance(item, str) for item in required_raw):
        raise ChannelInputError("form schema 'required' must be a list of property-name strings")
    return properties, set(required_raw)


def _canonical_hash_pages(
    schema: dict[str, Any], pages: list[dict[str, Any]], option_fields: set[str], flow_json: dict[str, Any]
) -> str:
    """sha256 hex over a canonical dump of ``(schema, pages, option_fields, flow_json)``.

    The published-Flow cache key for a per-send form. A different page layout, a
    different option-bearing set, OR a different emitted Flow shape keys a different
    published Flow; the ``schema`` member keeps the notify sidecar's exact-schema
    identity, and folding in the emitted ``flow_json`` means a change to the emitted
    shape alone re-keys (a corrected shape can never resolve an earlier one).
    """
    canonical = json.dumps(
        {"schema": schema, "pages": pages, "option_fields": sorted(option_fields), "flow": flow_json},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_form_properties(
    properties: dict[str, Any], required: set[str], option_fields: set[str], names: dict[str, str]
) -> None:
    """Validate every property is inside the supported per-send subset, before any screen is built.

    Raises ``ChannelInputError`` naming a reserved name, a non-object property, an unsupported type, or a
    string ``enum`` that is not a non-empty list of strings.
    """
    for name, prop in properties.items():
        if name == _RESERVED_PROPERTY:
            raise ChannelInputError(
                f"form property {name!r}: reserved on this channel — Meta injects {name!r} into the "
                "Flow response to correlate the reply, so a field of that name is unanswerable"
            )
        if not isinstance(prop, dict):
            raise ChannelInputError(f"form property {name!r}: schema must be an object")
        # A string enum, when present, must be a non-empty list of strings — else the
        # published dropdown would have nothing (or the wrong shape) to pick from.
        enum = prop.get("enum")
        if (
            prop.get("type") == "string"
            and enum is not None
            and (not isinstance(enum, list) or not enum or not all(isinstance(item, str) for item in enum))
        ):
            raise ChannelInputError(f"form property {name!r}: a string enum must be a non-empty list of strings")
        # Validate the subset up front (raises naming the property on an unsupported type). The bare
        # control is built: a field's second line and its Meta cap are checked where the field is
        # drawn, since a merged range end draws it as a caption rather than as helper text.
        _field_control(name, prop, name in required, option_fields, names)


@dataclass(frozen=True)
class _RenderSpec:
    """The cross-cutting inputs every screen reads while a Flow is built.

    ``field_changed`` are the schema keys whose change fires a reaction; ``page_advanced`` the
    page TITLES whose advance fires one; ``submitted`` whether the submission is reaction-checked.
    ``endpoint`` is True for an endpoint-driven Flow (a reacting form, or one carrying a shape no
    control checks natively) and drives the ``data_exchange`` footers + ``data_api_version``.
    ``slots`` maps each display slot to its screen-``data`` key. ``field_invalid`` is the caller's
    per-field ``invalid`` sentence; ``err_fields`` the fields binding an ``error-message``;
    ``invalid_fields`` the shaped fields whose sentence rides the Flow as ``__invalid`` (an ask-less
    endpoint-driven form only, else empty).
    """

    properties: dict[str, Any]
    required: set[str]
    option_fields: set[str]
    names: dict[str, str]
    labels: dict[str, str]
    slots: dict[str, str]
    field_changed: frozenset[str]
    page_advanced: frozenset[str]
    submitted: bool
    endpoint: bool
    # ``END name -> START name`` for every ``rangeStart`` pair drawn as ONE range control; a merged
    # END emits no control/data and a merged START carries the pair's object value.
    range_merges: dict[str, str]
    field_invalid: dict[str, str]
    err_fields: frozenset[str]
    invalid_fields: frozenset[str]


def _review_readback(spec: _RenderSpec) -> list[dict[str, Any]]:
    """Generic readback lines for a review screen: one ``TextBody`` per field, label + collected value.

    Each field's value rides forward as its ``__val`` carrier; the line interpolates it (Flow
    JSON ≥ 6.0 supports string concatenation within text), so the human sees every entered answer
    before submitting. The platform renders the readback from the schema labels — no per-form chrome.
    """
    return [
        {"type": "TextBody", "text": f"{spec.labels[name]}: ${{data.{spec.names[name]}__val}}"}
        for name in spec.properties
        if name not in spec.range_merges  # a merged END carries no value of its own
    ]


def _error_decl() -> dict[str, Any]:
    # The screen-``data`` declaration of a field's ``__err`` (its error-message text) or ``__invalid``.
    return {"type": "string", "__example__": ERROR_EXAMPLE}


def _screen_data_model(
    index: int, this_fields: list[str], later_fields: list[str], earlier_fields: list[str], spec: _RenderSpec
) -> dict[str, Any]:
    """The screen's ``data`` declarations.

    The entry screen declares EVERY field's init/ds/err (the send injects them all there); a later screen
    declares its own and its successors' init/ds/err plus a ``__val`` carrier for each field collected
    earlier. A merged range END declares only its ``__err`` (its START's range control binds it).
    Every screen of an ask-less endpoint-driven form declares each shaped field's ``__invalid``.
    """
    merged_ends = set(spec.range_merges)
    merged_starts = set(spec.range_merges.values())
    data_model: dict[str, Any] = {}
    rendered = list(spec.properties) if index == 0 else [*this_fields, *later_fields]
    for name in rendered:
        if name not in merged_ends:
            data_model.update(
                _field_data_decls(name, spec.properties[name], spec.option_fields, spec.names, name in merged_starts)
            )
        if name in spec.err_fields:
            data_model[f"{spec.names[name]}__err"] = _error_decl()
    if index > 0:
        for name in earlier_fields:
            if name in merged_ends:
                continue
            data_model[f"{spec.names[name]}__val"] = (
                _range_value_type_decl() if name in merged_starts else _value_type_decl(spec.properties[name])
            )
    for name in spec.properties:
        if name in spec.invalid_fields:
            data_model[f"{spec.names[name]}__invalid"] = _error_decl()
    return data_model


def _later_carriers(later_fields: list[str], spec: _RenderSpec) -> dict[str, str]:
    # The carriers each later (unrendered) field still needs on a later screen: its init/ds and its
    # ``__err`` (a merged END only its ``__err``), forwarded from this screen's data.
    merged_ends = set(spec.range_merges)
    forward: dict[str, str] = {}
    for name in later_fields:
        if name not in merged_ends:
            forward.update(_forward_field_data(name, spec.properties[name], spec.option_fields, spec.names))
        if name in spec.err_fields:
            forward[f"{spec.names[name]}__err"] = f"${{data.{spec.names[name]}__err}}"
    return forward


def _resolve_button_label(page: dict[str, Any]) -> str | None:
    """A page's ``button_label`` validated against Meta's ``Footer.label`` cap, or ``None`` when unset.

    The caller's own word for the page's primary button (advance or submit); absent, the footer
    shows its generic default. A label over :data:`_FOOTER_LABEL_MAX_CHARS` cannot ride Meta's
    Footer and is refused loudly at build (naming the page), never truncated.
    """
    label = page.get("button_label")
    if label is None:
        return None
    if not isinstance(label, str):
        raise ChannelInputError(f"form page {page.get('title')!r} button_label must be a string")
    if len(label) > _FOOTER_LABEL_MAX_CHARS:
        raise ChannelInputError(
            f"form page {page.get('title')!r} button_label is {len(label)} characters; WhatsApp caps a "
            f"footer label at {_FOOTER_LABEL_MAX_CHARS}"
        )
    return label


def _terminal_footer(
    this_fields: list[str],
    earlier_fields: list[str],
    spec: _RenderSpec,
    variants: dict[str, list[tuple[str, list[str]]]],
    button_label: str | None,
    screen_data: dict[str, str],
) -> dict[str, Any]:
    # The terminal screen's Footer, labelled by the page's ``button_label`` (else ``_FOOTER_LABEL``).
    # An endpoint-driven form fires a ``submitted`` data_exchange carrying the values and the
    # screen's whole data (the data endpoint checks the shapes and any declared submit reaction,
    # then completes or keeps the person on the screen, answering from that data); a
    # static form completes directly with the flat union of every field, keyed by human-readable
    # label. A merged range END emits no completion entry — its START carries the pair's object,
    # which the inbound decode splits.
    label = button_label or _FOOTER_LABEL
    if spec.endpoint:
        payload = event_payload(
            FormReactionEventKind.SUBMITTED,
            None,
            None,
            this_fields,
            earlier_fields,
            spec.names,
            variants,
            spec.range_merges,
        )
        payload.update(screen_data)
        return {
            "type": "Footer",
            "label": label,
            "on-click-action": {"name": "data_exchange", "payload": payload},
        }
    complete_payload = {
        spec.labels[name]: (
            form_ref(name, variants[name], spec.names) if name in this_fields else f"${{data.{spec.names[name]}__val}}"
        )
        for name in spec.properties
        if name not in spec.range_merges
    }
    return {"type": "Footer", "label": label, "on-click-action": {"name": "complete", "payload": complete_payload}}


def _step_footer(
    index: int,
    page_title: str,
    this_fields: list[str],
    later_fields: list[str],
    earlier_fields: list[str],
    spec: _RenderSpec,
    routing_model: dict[str, list[str]],
    variants: dict[str, list[tuple[str, list[str]]]],
    button_label: str | None,
    screen_data: dict[str, str],
) -> dict[str, Any]:
    # A non-terminal screen's Footer, labelled by the page's ``button_label`` (else
    # ``_CONTINUE_LABEL``). An endpoint-driven form fires a ``page_advanced`` data_exchange carrying
    # the values so far and the screen's whole data, which holds the later fields' carriers (the
    # data endpoint checks this screen's shapes and any declared advance reaction, then names the
    # next screen or keeps the person here); a static form navigates directly, forwarding
    # successors' carriers and every collected value. A merged range END forwards no value — its
    # START carries the pair's object value.
    # Either way the transition is recorded in ``routing_model``.
    label = button_label or _CONTINUE_LABEL
    merged_ends = set(spec.range_merges)
    screen_id = _screen_id(index)
    next_screen = _screen_id(index + 1)
    routing_model[screen_id] = [next_screen]
    if spec.endpoint:
        payload = event_payload(
            FormReactionEventKind.PAGE_ADVANCED,
            REACTION_PAGE_KEY,
            page_title,
            this_fields,
            earlier_fields,
            spec.names,
            variants,
            spec.range_merges,
        )
        payload.update(screen_data)
        return {
            "type": "Footer",
            "label": label,
            "on-click-action": {"name": "data_exchange", "payload": payload},
        }
    forward = _later_carriers(later_fields, spec)
    for name in earlier_fields:
        if name in merged_ends:
            continue
        forward[f"{spec.names[name]}__val"] = f"${{data.{spec.names[name]}__val}}"
    for name in this_fields:
        if name in merged_ends:
            continue
        forward[f"{spec.names[name]}__val"] = form_ref(name, variants[name], spec.names)
    return {
        "type": "Footer",
        "label": label,
        "on-click-action": {"name": "navigate", "next": {"type": "screen", "name": next_screen}, "payload": forward},
    }


def _build_form_screen(
    index: int,
    page: dict[str, Any],
    spec: _RenderSpec,
    fields_by_screen: list[list[str]],
    earlier_fields: list[str],
    screen_count: int,
    routing_model: dict[str, list[str]],
) -> dict[str, Any]:
    """One Flow screen for a page: its display blocks, field components, review readback, and footer.

    Display blocks render in declared order ahead of the inputs; each control draws its native shapes
    and binds its ``error-message``; a reacting field carries a ``data_exchange`` action; a
    ``visibleWhen`` field renders under client-side ``If``s — one per
    matching value when shown for several (Meta's ``||`` refuses a disjunction of equality
    comparisons), each case uniquely named and its answer coalesced back under the field's own name
    (:func:`screen_field_variants`, :func:`emit_field_variants`, :func:`form_ref`); a review page adds
    the generic value readback. The footer records any transition in ``routing_model``.
    """
    this_fields = fields_by_screen[index]
    later_fields = [field for screen in fields_by_screen[index + 1 :] for field in screen]
    is_terminal = index == screen_count - 1
    page_title = str(page["title"])
    button_label = _resolve_button_label(page)

    data_model = _screen_data_model(index, this_fields, later_fields, earlier_fields, spec)
    for block in page.get("display") or []:
        slot = block.get("slot") if isinstance(block, dict) else None
        if isinstance(slot, str):
            data_model[spec.slots[slot]] = {"type": "string", "__example__": ""}
    screen_data = screen_data_refs(data_model) if spec.endpoint else {}

    variants = screen_field_variants(this_fields, earlier_fields, spec.properties, spec.names)
    display_blocks = _display_component_list(page, spec.slots)
    merged_ends = set(spec.range_merges)
    merged_starts = {start: end for end, start in spec.range_merges.items()}
    components: list[dict[str, Any]] = []
    for name in this_fields:
        if name in merged_ends:
            # A merged range END emits no control of its own — its START draws the one range control.
            continue
        if name in merged_starts:
            end = merged_starts[name]
            component = _calendar_range_component(
                name,
                end,
                spec.properties[name],
                spec.properties[end],
                name in spec.required,
                end in spec.required,
                spec.names[name],
                spec.names[end],
            )
            # A field_changed on EITHER end attaches the one on-select-action, named by the end that
            # declared the trigger.
            if name in spec.field_changed or end in spec.field_changed:
                trigger_field = name if name in spec.field_changed else end
                component = _attach_field_reaction(
                    component,
                    trigger_field,
                    this_fields,
                    earlier_fields,
                    spec.names,
                    variants,
                    spec.range_merges,
                    screen_data,
                )
            captions = range_captions(name, spec.properties[name], end, spec.properties[end])
            components.extend(emit_field_variants(component, variants[name], captions))
            continue
        component = _dynamic_component(
            name,
            spec.properties[name],
            name in spec.required,
            spec.option_fields,
            spec.names,
            spec.field_invalid.get(name),
        )
        caption = field_caption(name, component, spec.properties[name])
        if name in spec.field_changed:
            component = _attach_field_reaction(
                component, name, this_fields, earlier_fields, spec.names, variants, spec.range_merges, screen_data
            )
        components.extend(emit_field_variants(component, variants[name], caption))
    readback = _review_readback(spec) if page.get("kind") == "review" else []
    footer = (
        _terminal_footer(this_fields, earlier_fields, spec, variants, button_label, screen_data)
        if is_terminal
        else _step_footer(
            index,
            page_title,
            this_fields,
            later_fields,
            earlier_fields,
            spec,
            routing_model,
            variants,
            button_label,
            screen_data,
        )
    )

    screen: dict[str, Any] = {
        "id": _screen_id(index),
        "title": page_title,
        "terminal": is_terminal,
        "layout": {"type": "SingleColumnLayout", "children": [*display_blocks, *components, *readback, footer]},
    }
    if data_model:
        screen["data"] = data_model
    return screen


def form_needs_endpoint(
    schema: dict[str, Any],
    pages: list[dict[str, Any]] | None = None,
    option_fields: set[str] | None = None,
    reactions: dict[str, Any] | None = None,
) -> bool:
    """Whether this form is published ENDPOINT-DRIVEN: it reacts, or a field's shape is not native.

    The same decision :func:`build_form_flow` draws the Flow by — a form declaring a consumer
    reaction, or carrying a shape no drawn Meta control checks
    (:func:`~tai42_channel_whatsapp.flows_shapes.field_needs_endpoint`), goes through the data
    endpoint. Pure. Raises ``ChannelInputError`` as :func:`build_form_flow` does for a schema
    outside the subset.
    """
    option_fields = option_fields or set()
    properties, required = _validate_object_schema(schema)
    names = component_names(properties)
    _validate_form_properties(properties, required, option_fields, names)
    resolved_pages, fields_by_screen = _resolve_pages(properties, pages)
    reacting = _reaction_sets(reactions, properties, option_fields, names)[3]
    range_merges = _form_range_merges(properties, resolved_pages, fields_by_screen)
    return form_endpoint_driven(properties, required, option_fields, names, range_merges, reacting)


def build_form_flow(
    schema: dict[str, Any],
    pages: list[dict[str, Any]] | None = None,
    option_fields: set[str] | None = None,
    reactions: dict[str, Any] | None = None,
    form_title: str | None = None,
    form_submit: str | None = None,
    field_invalid: dict[str, str] | None = None,
    carry_invalid: bool = False,
) -> tuple[dict[str, Any], str]:
    """The ``(flow_json, key)`` for a per-send/stepped form ask — publishable Flow JSON.

    One screen per page (``pages`` absent → one screen carrying every property in
    schema order); each screen holds its display blocks, field components and footer directly
    (no ``Form`` wrapper) and its ``id`` is letters-and-underscores only. Each schema property
    is mapped to a unique identifier-safe component name (:func:`component_names`), which
    is what every component ``name``, data key, reference and navigate-payload key uses, so
    a property named with any character still yields a publishable Flow; the terminal
    completion payload is keyed instead by each property's human-readable label
    (:func:`payload_labels`), its values still the component references. Each choice field
    reads a dynamic ``data-source`` and every control an ``init-value``, so the send
    supplies the values/options through ``flow_action_payload.data``. An array of strings
    renders a ``CheckboxGroup``; a string with a short schema ``enum`` a ``RadioButtonsGroup``
    (else a ``Dropdown`` — also when ``option_fields`` names an option-bearing string); a
    date field a single-mode ``CalendarPicker``; any other string a ``TextInput``. Each control draws
    the shapes Meta checks natively and binds its ``error-message``. A page's ``display`` blocks render ahead
    of its inputs, a ``review`` page adds a generic value readback, and a ``visibleWhen``
    field renders under client-side ``If``s — one per matching value when shown for several, each
    uniquely named with its answer coalesced back under the field's own name.

    ``reactions`` (optional, a :class:`FormReactions` dump) or a field shape no control checks
    natively makes the Flow ENDPOINT-DRIVEN (:func:`form_needs_endpoint`): it carries
    ``data_api_version`` + a ``routing_model``, every footer fires a ``data_exchange`` the data
    endpoint answers (shape check, then any declared reaction), and the reacting fields fire their
    own. Otherwise the Flow is the static navigate Flow. ``field_invalid`` is the caller's per-field
    ``invalid`` sentence (the helper text a native pattern requires when the field has no second
    line); ``carry_invalid`` (an ask-less endpoint-driven form, which has no per-send sidecar) makes
    every shaped field's sentence ride the Flow as ``__invalid`` screen data.

    ``key`` is the hash of the ``(schema, pages, option_fields)`` and the emitted ``flow_json``
    — which folds in the reactions/display/date-constraint/``visibleWhen``/shape layout, so a
    changed form re-publishes. Pure — no I/O. Raises ``ChannelInputError`` naming any property
    outside the subset or any page field that is not a declared property.
    """
    option_fields = option_fields or set()
    properties, required = _validate_object_schema(schema)
    names = component_names(properties)
    labels = payload_labels(properties)
    _validate_form_properties(properties, required, option_fields, names)
    resolved_pages, fields_by_screen = _resolve_pages(properties, pages, form_title, form_submit)
    field_changed, page_advanced, submitted, reacting = _reaction_sets(reactions, properties, option_fields, names)
    range_merges = _form_range_merges(properties, resolved_pages, fields_by_screen)
    endpoint = form_endpoint_driven(properties, required, option_fields, names, range_merges, reacting)
    slots = slot_datanames(resolved_pages)
    if endpoint:
        guard_endpoint_names(properties, names, slots.values())
    spec = _RenderSpec(
        properties=properties,
        required=required,
        option_fields=option_fields,
        names=names,
        labels=labels,
        slots=slots,
        field_changed=field_changed,
        page_advanced=page_advanced,
        submitted=submitted,
        endpoint=endpoint,
        range_merges=range_merges,
        field_invalid=dict(field_invalid or {}),
        err_fields=error_fields(properties, required, option_fields, names, set(range_merges)),
        invalid_fields=shaped_fields(properties) if endpoint and carry_invalid else frozenset(),
    )

    screen_count = len(resolved_pages)
    screens: list[dict[str, Any]] = []
    routing_model: dict[str, list[str]] = {}
    earlier_fields: list[str] = []
    for index, page in enumerate(resolved_pages):
        screens.append(
            _build_form_screen(index, page, spec, fields_by_screen, earlier_fields, screen_count, routing_model)
        )
        earlier_fields = [*earlier_fields, *fields_by_screen[index]]

    flow_json: dict[str, Any] = {"version": _FLOW_JSON_VERSION, "screens": screens}
    if endpoint:
        # An endpoint-driven Flow declares the data-exchange API version it speaks.
        flow_json["data_api_version"] = _DATA_API_VERSION
    if screen_count > 1:
        # A multi-screen flow declares its allowed transitions.
        routing_model[_screen_id(screen_count - 1)] = []
        flow_json["routing_model"] = routing_model
    elif endpoint:
        # A single-screen endpoint-driven Flow still declares a (terminal) routing model.
        flow_json["routing_model"] = {_screen_id(0): []}
    return flow_json, _canonical_hash_pages(schema, resolved_pages, option_fields, flow_json)
