"""Answer-schema → WhatsApp Flow JSON mapping.

An ``ask`` form ask carries a JSON answer schema; this module renders it as a
publishable WhatsApp Flow the human fills in-chat. The supported subset is a
top-level ``{"type": "object", "properties": {...}, "required": [...]}`` whose
properties map one-to-one onto Flow field components:

* ``string``                    → ``TextInput``
* ``string`` with ``enum``      → ``Dropdown`` (dynamic ``data-source`` items)
* ``string`` + ``format: date`` → ``DatePicker`` (sets/returns a ``YYYY-MM-DD`` string)
* ``string`` + ``format: time`` / ``date-time`` → ``TextInput`` (free text; the vendor has no
  time-of-day picker, and its ``CalendarPicker`` is a calendar, not a date-time control)
* ``boolean``                   → ``OptIn``
* ``integer`` / ``number``      → ``TextInput`` with ``input-type: "number"``

``format`` is part of the platform's form-schema subset (validated at the ask door); this
module only chooses the WhatsApp control for it. A ``DatePicker`` carries no ``required`` field
— the vendor's component reference defines one for every other input control but not for the
``DatePicker`` — so a schema's required flag on a date field is not enforced by that control.
An enum or option-bearing string stays a ``Dropdown`` even with ``format: date``: an explicit
choice list outranks a format hint.

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

from tai42_channel_whatsapp.flows_components import (
    _RESERVED_PROPERTY,
    RADIO_OPTION_THRESHOLD,
    _choice_options_enum,
    _display_component_list,
    _dynamic_component,
    _field_data_decls,
    _forward_field_data,
    _is_array_of_strings,
    _is_choice_field,
    _is_date_field,
    _sanitize_name,
    _valid_iso_date,
    _value_type_decl,
    component_names,
    emit_field_variants,
    form_ref,
    payload_labels,
    screen_field_variants,
)

__all__ = [
    "FORM_ENTRY_SCREEN",
    "RADIO_OPTION_THRESHOLD",
    "SUCCESS_SCREEN",
    "build_flow_data",
    "build_form_flow",
    "component_names",
    "payload_labels",
    "slot_datanames",
]

# Flow JSON version pinned to a Cloud-API-valid release. This is a static
# (endpoint-less) navigate flow whose terminal screen completes with the form
# payload — no ``data_api_version`` because there is no data-exchange endpoint.
# Bump this constant when a newer schema version is adopted.
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
# Generic labels — a form ask carries no domain-specific chrome.
_SCREEN_TITLE = "Form"
_FOOTER_LABEL = "Submit"
# The non-terminal step's footer — advances to the next screen.
_CONTINUE_LABEL = "Continue"

# The data-exchange API version an endpoint-driven (reacting) Flow declares in its Flow JSON.
# A static (navigate-only) Flow carries none. Meta's current data-endpoint version.
_DATA_API_VERSION = "3.0"

# The ``visibleWhen`` predicate operators (mirrors the kit) — a property carries exactly one.
_VISIBLE_WHEN_OPS = ("equals", "in", "notEmpty")

# The reaction-event markers an endpoint-driven Flow carries in a ``data_exchange`` action's
# payload so the data endpoint maps the vendor request onto a ``react(...)`` event. They are
# reserved property names on a REACTING form (refused like ``flow_token``) so a field value can
# never collide with a marker in the same payload; a static form never carries them.
_REACTION_EVENT_KEY = "tai42_event"
_REACTION_FIELD_KEY = "tai42_field"
_REACTION_PAGE_KEY = "tai42_page"
_RESERVED_REACTION_KEYS = frozenset({_REACTION_EVENT_KEY, _REACTION_FIELD_KEY, _REACTION_PAGE_KEY})

# The three reaction event kinds (mirrors the contract ``FormReactions`` triggers).
_EVENT_FIELD_CHANGED = "field_changed"
_EVENT_PAGE_ADVANCED = "page_advanced"
_EVENT_SUBMITTED = "submitted"

# The screen a data-endpoint response names to COMPLETE a reacting Flow (Meta's reserved
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
        # Validate the subset up front (raises naming the property on an unsupported type).
        _dynamic_component(name, prop, name in required, option_fields, names)


def _resolve_pages(
    properties: dict[str, Any], pages: list[dict[str, Any]] | None
) -> tuple[list[dict[str, Any]], list[list[str]]]:
    """The resolved page list and each page's field names.

    Defaults to one screen carrying every property in schema order. Raises ``ChannelInputError`` for a
    page that names a property the schema does not declare.
    """
    resolved_pages = pages or [{"title": _SCREEN_TITLE, "fields": list(properties)}]
    fields_by_screen: list[list[str]] = []
    for page in resolved_pages:
        page_fields = [str(field) for field in page["fields"]]
        for field in page_fields:
            if field not in properties:
                raise ChannelInputError(f"form page names unknown property {field!r}")
        fields_by_screen.append(page_fields)
    return resolved_pages, fields_by_screen


@dataclass(frozen=True)
class _RenderSpec:
    """The cross-cutting inputs every screen reads while a Flow is built.

    ``field_changed`` are the schema keys whose change fires a reaction; ``page_advanced`` the
    page TITLES whose advance fires one; ``submitted`` whether the submission is reaction-checked.
    ``endpoint`` is True for a reacting (endpoint-driven) Flow and drives the ``data_exchange``
    actions + ``data_api_version``. ``slots`` maps each display slot to its screen-``data`` key.
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


def slot_datanames(pages: list[dict[str, Any]] | None) -> dict[str, str]:
    """Map each declared display ``slot`` to a unique identifier-safe screen-``data`` key.

    A slotted display block reads ``${data.<key>}``; a reaction's ``display`` update fills it
    through the data endpoint. Slot names are unique across a form (the contract enforces it);
    each is sanitised to Meta's identifier grammar and disambiguated deterministically on a
    sanitisation collision, so the renderer and the data endpoint recompute an identical map.
    """
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for page in pages or []:
        for block in page.get("display") or []:
            slot = block.get("slot") if isinstance(block, dict) else None
            if not isinstance(slot, str) or slot in mapping:
                continue
            base = f"slot_{_sanitize_name(slot)}"
            candidate = base
            suffix = 2
            while candidate in used:
                candidate = f"{base}_{suffix}"
                suffix += 1
            used.add(candidate)
            mapping[slot] = candidate
    return mapping


def _review_readback(spec: _RenderSpec) -> list[dict[str, Any]]:
    """Generic readback lines for a review screen: one ``TextBody`` per field, label + collected value.

    Each field's value rides forward as its ``__val`` carrier; the line interpolates it (Flow
    JSON ≥ 6.0 supports string concatenation within text), so the human sees every entered answer
    before submitting. The platform renders the readback from the schema labels — no per-form chrome.
    """
    return [
        {"type": "TextBody", "text": f"{spec.labels[name]}: ${{data.{spec.names[name]}__val}}"}
        for name in spec.properties
    ]


def _reaction_event_payload(
    kind: str,
    marker_key: str | None,
    marker_value: str | None,
    this_fields: list[str],
    earlier_fields: list[str],
    names: dict[str, str],
    variants: dict[str, list[tuple[str, list[str]]]],
) -> dict[str, str]:
    """A ``data_exchange`` action payload: the event markers plus the values filled so far.

    The event kind rides ``tai42_event``; a field/page trigger its ``tai42_field``/``tai42_page``
    marker. Each known value is keyed by its SCHEMA NAME (this screen's through its coalesced
    ``form_ref`` — a single control, or a backtick concatenation of a split field's variants —
    earlier ones through their ``__val`` carrier), so the data endpoint hands the react facet
    partial values keyed exactly as the stored schema. Later (unfilled) fields are omitted.
    """
    payload: dict[str, str] = {_REACTION_EVENT_KEY: kind}
    if marker_key is not None and marker_value is not None:
        payload[marker_key] = marker_value
    for name in this_fields:
        payload[name] = form_ref(name, variants[name], names)
    for name in earlier_fields:
        payload[name] = f"${{data.{names[name]}__val}}"
    return payload


def _attach_field_reaction(
    component: dict[str, Any],
    name: str,
    this_fields: list[str],
    earlier_fields: list[str],
    names: dict[str, str],
    variants: dict[str, list[tuple[str, list[str]]]],
) -> dict[str, Any]:
    """Attach a ``field_changed`` ``data_exchange`` action to a selectable control, or raise.

    A choice/date control carries the action on ``on-select-action``, an OptIn on
    ``on-click-action``; a plain text or numeric input has no on-change event on WhatsApp and is
    refused loudly (never silently dropped).
    """
    action = {
        "name": "data_exchange",
        "payload": _reaction_event_payload(
            _EVENT_FIELD_CHANGED, _REACTION_FIELD_KEY, name, this_fields, earlier_fields, names, variants
        ),
    }
    ctype = component["type"]
    if ctype in ("Dropdown", "RadioButtonsGroup", "CheckboxGroup", "CalendarPicker", "DatePicker"):
        component["on-select-action"] = action
    elif ctype == "OptIn":
        component["on-click-action"] = action
    else:
        raise ChannelInputError(
            f"form property {name!r}: a field_changed reaction needs a selectable control (a choice, "
            f"date, or opt-in); a {ctype} has no on-change event on WhatsApp"
        )
    return component


def _screen_data_model(
    index: int, this_fields: list[str], later_fields: list[str], earlier_fields: list[str], spec: _RenderSpec
) -> dict[str, Any]:
    """The screen's ``data`` declarations.

    The entry screen declares EVERY field's init/ds (the send injects them all there); a later screen
    declares its own and its successors' init/ds plus a ``__val`` carrier for each field collected earlier.
    """
    data_model: dict[str, Any] = {}
    if index == 0:
        for name, prop in spec.properties.items():
            data_model.update(_field_data_decls(name, prop, spec.option_fields, spec.names))
    else:
        for name in [*this_fields, *later_fields]:
            data_model.update(_field_data_decls(name, spec.properties[name], spec.option_fields, spec.names))
        for name in earlier_fields:
            data_model[f"{spec.names[name]}__val"] = _value_type_decl(spec.properties[name])
    return data_model


def _terminal_footer(
    this_fields: list[str],
    earlier_fields: list[str],
    spec: _RenderSpec,
    variants: dict[str, list[tuple[str, list[str]]]],
) -> dict[str, Any]:
    # The terminal screen's Footer. A reacting form whose submission is reaction-checked fires a
    # ``submitted`` data_exchange (the data endpoint accepts or refuses, then completes); a static
    # form completes directly with the flat union of every field, keyed by human-readable label.
    if spec.submitted:
        payload = _reaction_event_payload(
            _EVENT_SUBMITTED, None, None, this_fields, earlier_fields, spec.names, variants
        )
        return {
            "type": "Footer",
            "label": _FOOTER_LABEL,
            "on-click-action": {"name": "data_exchange", "payload": payload},
        }
    payload = {
        spec.labels[name]: (
            form_ref(name, variants[name], spec.names) if name in this_fields else f"${{data.{spec.names[name]}__val}}"
        )
        for name in spec.properties
    }
    return {"type": "Footer", "label": _FOOTER_LABEL, "on-click-action": {"name": "complete", "payload": payload}}


def _step_footer(
    index: int,
    page_title: str,
    this_fields: list[str],
    later_fields: list[str],
    earlier_fields: list[str],
    spec: _RenderSpec,
    routing_model: dict[str, list[str]],
    variants: dict[str, list[tuple[str, list[str]]]],
) -> dict[str, Any]:
    # A non-terminal screen's Footer. A page whose advance is reaction-triggered fires a
    # ``page_advanced`` data_exchange (the data endpoint applies the reaction and names the next
    # screen); any other page navigates directly, forwarding successors' init/ds and every
    # collected value. Either way the transition is recorded in ``routing_model``.
    screen_id = _screen_id(index)
    next_screen = _screen_id(index + 1)
    routing_model[screen_id] = [next_screen]
    if page_title in spec.page_advanced:
        payload = _reaction_event_payload(
            _EVENT_PAGE_ADVANCED, _REACTION_PAGE_KEY, page_title, this_fields, earlier_fields, spec.names, variants
        )
        return {
            "type": "Footer",
            "label": _CONTINUE_LABEL,
            "on-click-action": {"name": "data_exchange", "payload": payload},
        }
    forward: dict[str, str] = {}
    for name in later_fields:
        forward.update(_forward_field_data(name, spec.properties[name], spec.option_fields, spec.names))
    for name in earlier_fields:
        forward[f"{spec.names[name]}__val"] = f"${{data.{spec.names[name]}__val}}"
    for name in this_fields:
        forward[f"{spec.names[name]}__val"] = form_ref(name, variants[name], spec.names)
    return {
        "type": "Footer",
        "label": _CONTINUE_LABEL,
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

    Display blocks render in declared order ahead of the inputs; a reacting field carries a
    ``data_exchange`` action; a ``visibleWhen`` field renders under client-side ``If``s — one per
    matching value when shown for several (Meta's ``||`` refuses a disjunction of equality
    comparisons), each case uniquely named and its answer coalesced back under the field's own name
    (:func:`screen_field_variants`, :func:`emit_field_variants`, :func:`form_ref`); a review page adds
    the generic value readback. The footer records any transition in ``routing_model``.
    """
    this_fields = fields_by_screen[index]
    later_fields = [field for screen in fields_by_screen[index + 1 :] for field in screen]
    is_terminal = index == screen_count - 1
    page_title = str(page["title"])

    data_model = _screen_data_model(index, this_fields, later_fields, earlier_fields, spec)
    for block in page.get("display") or []:
        slot = block.get("slot") if isinstance(block, dict) else None
        if isinstance(slot, str):
            data_model[spec.slots[slot]] = {"type": "string", "__example__": ""}

    variants = screen_field_variants(this_fields, earlier_fields, spec.properties, spec.names)
    display_blocks = _display_component_list(page, spec.slots)
    components: list[dict[str, Any]] = []
    for name in this_fields:
        component = _dynamic_component(
            name, spec.properties[name], name in spec.required, spec.option_fields, spec.names
        )
        if name in spec.field_changed:
            component = _attach_field_reaction(component, name, this_fields, earlier_fields, spec.names, variants)
        components.extend(emit_field_variants(component, variants[name]))
    readback = _review_readback(spec) if page.get("kind") == "review" else []
    footer = (
        _terminal_footer(this_fields, earlier_fields, spec, variants)
        if is_terminal
        else _step_footer(index, page_title, this_fields, later_fields, earlier_fields, spec, routing_model, variants)
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


def _reaction_sets(
    reactions: dict[str, Any] | None, properties: dict[str, Any], option_fields: set[str], names: dict[str, str]
) -> tuple[frozenset[str], frozenset[str], bool, bool]:
    """The ``(field_changed, page_advanced, submitted, endpoint)`` render triggers from a reactions block.

    ``endpoint`` is True when any trigger is declared (the Flow is then endpoint-driven). A
    ``field_changed`` field must render as a selectable control (refused loudly otherwise), and a
    reacting form may not name a property that collides with a reserved reaction marker key.
    """
    if not reactions:
        return frozenset(), frozenset(), False, False
    field_changed = frozenset(str(field) for field in reactions.get("field_changed", []))
    page_advanced = frozenset(str(page) for page in reactions.get("page_advanced", []))
    submitted = bool(reactions.get("submitted", False))
    endpoint = bool(field_changed or page_advanced or submitted)
    if endpoint:
        collision = _RESERVED_REACTION_KEYS & set(properties)
        if collision:
            raise ChannelInputError(
                f"reacting form property names {sorted(collision)} collide with the reserved reaction "
                f"markers {sorted(_RESERVED_REACTION_KEYS)}"
            )
        for name in field_changed:
            prop = properties.get(name)
            if not isinstance(prop, dict):
                raise ChannelInputError(f"form reaction field_changed names unknown property {name!r}")
            ctype = _dynamic_component(name, prop, False, option_fields, names)["type"]
            if ctype not in ("Dropdown", "RadioButtonsGroup", "CheckboxGroup", "CalendarPicker", "DatePicker", "OptIn"):
                raise ChannelInputError(
                    f"form property {name!r}: a field_changed reaction needs a selectable control (a choice, "
                    f"date, or opt-in); a {ctype} has no on-change event on WhatsApp"
                )
    return field_changed, page_advanced, submitted, endpoint


def build_form_flow(
    schema: dict[str, Any],
    pages: list[dict[str, Any]] | None = None,
    option_fields: set[str] | None = None,
    reactions: dict[str, Any] | None = None,
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
    date field with platform constraints a bounded ``CalendarPicker``, a plain date a bare
    ``DatePicker``; any other string a ``TextInput``. A page's ``display`` blocks render ahead
    of its inputs, a ``review`` page adds a generic value readback, and a ``visibleWhen``
    field renders under client-side ``If``s — one per matching value when shown for several, each
    uniquely named with its answer coalesced back under the field's own name.

    ``reactions`` (optional, a :class:`FormReactions` dump) makes the Flow ENDPOINT-DRIVEN: it
    carries ``data_api_version`` + a ``routing_model`` and the reacting fields/pages/submission
    fire ``data_exchange`` actions the data endpoint routes to the reaction facet. Absent (or
    with no trigger) the Flow is the static navigate Flow exactly as before.

    ``key`` is the hash of the ``(schema, pages, option_fields)`` and the emitted ``flow_json``
    — which now folds in the reactions/display/date-constraint/``visibleWhen`` shape, so a
    changed form re-publishes. Pure — no I/O. Raises ``ChannelInputError`` naming any property
    outside the subset or any page field that is not a declared property.
    """
    option_fields = option_fields or set()
    properties, required = _validate_object_schema(schema)
    names = component_names(properties)
    labels = payload_labels(properties)
    _validate_form_properties(properties, required, option_fields, names)
    resolved_pages, fields_by_screen = _resolve_pages(properties, pages)
    field_changed, page_advanced, submitted, endpoint = _reaction_sets(reactions, properties, option_fields, names)
    spec = _RenderSpec(
        properties=properties,
        required=required,
        option_fields=option_fields,
        names=names,
        labels=labels,
        slots=slot_datanames(resolved_pages),
        field_changed=field_changed,
        page_advanced=page_advanced,
        submitted=submitted,
        endpoint=endpoint,
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


def build_flow_data(
    schema: dict[str, Any],
    values: dict[str, Any] | None,
    options: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    """The ``flow_action_payload.data`` a per-send form carries.

    Every field's ``init`` (its prefilled value, or the empty default) and every choice field's ``ds`` (its
    per-send option list ``{id, title}``, or the schema ``enum`` as the default) — keyed by the field's
    identifier-safe component name (:func:`component_names`), matching the published Flow's data keys.

    A choice field is a string property that carries a schema ``enum`` OR one the send
    marks option-bearing (a key in ``options``), OR an array of strings (a CheckboxGroup) —
    matching the published Flow's dynamic choice controls. A per-send option list keyed on a
    property that is neither a string nor an array of strings cannot be honored and is refused
    loudly, naming the field. A prefill on a ``format: date`` field that is not a valid
    ``YYYY-MM-DD`` string is refused loudly too — that is the only shape Meta's date picker
    accepts — before any network work. An array field's ``init`` is the list of selected
    values (``[]`` when unfilled); a boolean's a bool; every other field's a string. Raises
    ``ChannelInputError``.
    """
    values = values or {}
    options = options or {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ChannelInputError("form schema must carry a non-empty 'properties' object")
    names = component_names(properties)

    for name in options:
        prop = properties.get(name)
        if not isinstance(prop, dict) or not (prop.get("type") == "string" or _is_array_of_strings(prop)):
            raise ChannelInputError(
                f"form field {name!r}: WhatsApp cannot apply per-send options to this property "
                "— only a string or an array of strings maps to a choice control"
            )

    option_fields = set(options)
    data: dict[str, Any] = {}
    for name, prop in properties.items():
        cname = names[name]
        prop_type = prop.get("type") if isinstance(prop, dict) else None
        if name in values:
            raw = values[name]
            if _is_date_field(prop) and not (isinstance(raw, str) and _valid_iso_date(raw)):
                raise ChannelInputError(
                    f"form property {name!r}: a date field prefill must be a 'YYYY-MM-DD' string "
                    f"(the shape Meta's date picker sets and returns), got {raw!r}"
                )
            data[f"{cname}__init"] = _init_value(prop_type, raw)
        else:
            data[f"{cname}__init"] = [] if prop_type == "array" else False if prop_type == "boolean" else ""
        if isinstance(prop, dict) and _is_choice_field(name, prop, option_fields):
            data[f"{cname}__ds"] = _choice_data_source(name, prop, options)
    return data


def _init_value(prop_type: Any, raw: Any) -> Any:
    """A prefilled value shaped for its control's ``init-value``.

    A list for an array (CheckboxGroup), a bool for an OptIn, else a string.
    """
    if prop_type == "boolean":
        return bool(raw)
    if prop_type == "array":
        return raw if isinstance(raw, list) else [str(raw)]
    return raw if isinstance(raw, str) else str(raw)


def _choice_data_source(
    name: str, prop: dict[str, Any], options: dict[str, list[dict[str, Any]]]
) -> list[dict[str, str]]:
    """A choice field's per-send ``data-source`` — the explicit per-send option list, else the schema enum.

    Raises ``ChannelInputError`` when neither is available (a choice control with no options to show).
    """
    if name in options:
        return [{"id": choice["value"], "title": choice.get("label") or choice["value"]} for choice in options[name]]
    enum = _choice_options_enum(prop)
    if enum is None:
        raise ChannelInputError(
            f"form property {name!r}: a choice control needs an enum or per-send options to populate its data-source"
        )
    return [{"id": str(item), "title": str(item)} for item in enum]
