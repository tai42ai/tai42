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

import datetime
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from tai42_contract.channels import ChannelInputError

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

# Meta injects ``flow_token`` into every Flow response to correlate the reply, so a
# form property of that name is unanswerable on this channel: the reply handler
# strips the key before the answer reaches the door. The mapper refuses it up front.
_RESERVED_PROPERTY = "flow_token"

# Meta's grammar for a Flow component ``name`` / screen-``data`` key / ``${data.…}``
# reference: a letter or underscore, then letters, digits, underscores.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Meta's ``DatePicker`` (Flow JSON ≥ 5.0) sets and returns a ``YYYY-MM-DD`` string. A date
# field's prefill must be exactly that shape, so the emitted ``init-value`` is a value the
# control accepts. The regex fixes the hyphenated calendar-date form; the ``date.fromisoformat``
# check then rejects an impossible date (e.g. ``2026-13-40``).
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: The option-count threshold a choice field uses to choose a RadioButtonsGroup over a
#: Dropdown — an ``enum`` of at most this many choices renders as a radio group, more as a
#: dropdown. Mirrors the kit's single documented constant so every surface draws the same
#: short list the same way (the plugin sits below the skeleton and cannot import it).
RADIO_OPTION_THRESHOLD = 5

# The data-exchange API version an endpoint-driven (reacting) Flow declares in its Flow JSON.
# A static (navigate-only) Flow carries none. Meta's current data-endpoint version.
_DATA_API_VERSION = "3.0"

# The platform date-constraint keys a date property may carry (mirrors the kit's
# ``form_schema`` keys): bounds, excluded days, and a two-field range pairing. Their presence
# makes a date field render as a bounded CalendarPicker rather than a bare DatePicker.
_DATE_CONSTRAINT_KEYS = ("minDate", "maxDate", "unavailableDates", "rangeStart", "minDays", "maxDays")

# The lowercase weekday names an ``unavailableDates`` entry may carry, in week order, and
# their mapping onto Meta's CalendarPicker ``include-days`` enum codes. A weekday excluded by
# ``unavailableDates`` is dropped from ``include-days`` (the days that STAY selectable).
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_CODES = {
    "monday": "Mon",
    "tuesday": "Tue",
    "wednesday": "Wed",
    "thursday": "Thu",
    "friday": "Fri",
    "saturday": "Sat",
    "sunday": "Sun",
}

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


def _is_date_field(prop: Any) -> bool:
    """Whether a property renders as a ``DatePicker`` — a ``string`` carrying ``"format": "date"``.

    ``format: time`` and ``format: date-time`` are not this: the vendor has no time-of-day
    picker, so they stay a ``TextInput``.
    """
    return isinstance(prop, dict) and prop.get("type") == "string" and prop.get("format") == "date"


def _valid_iso_date(value: str) -> bool:
    """Whether ``value`` is a ``YYYY-MM-DD`` calendar date the ``DatePicker`` accepts."""
    if not _ISO_DATE_RE.match(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _sanitize_name(key: str) -> str:
    """A property name reduced to Meta's identifier grammar.

    Every character outside ``[A-Za-z0-9_]`` becomes ``_``; a result that is empty or
    starts with a digit is prefixed ``f_`` so it opens with a letter or underscore.
    """
    reduced = re.sub(r"[^A-Za-z0-9_]", "_", key)
    if not reduced or reduced[0].isdigit():
        reduced = f"f_{reduced}"
    return reduced


def component_names(properties: dict[str, Any]) -> dict[str, str]:
    """Map each schema property name to a unique identifier-safe Flow component name.

    Meta accepts a component ``name``, a data key and a ``${data.…}`` reference only in
    the grammar ``[A-Za-z_][A-Za-z0-9_]*``, while a form's answer schema may name a
    property with any character. A key already in the grammar is kept verbatim; any
    other is sanitised by :func:`_sanitize_name`. A collision — two keys reducing to the
    same name, a sanitised name equal to a later verbatim key, or a name equal to the
    reserved ``flow_token`` — is disambiguated deterministically in schema order by
    appending ``_2``, ``_3`` … so the first key to claim a name keeps it. Pure and
    order-preserving: the same property list always yields the same map, so a re-send
    and the inbound decode recompute an identical map from the same schema.
    """
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for key in properties:
        base = key if _IDENTIFIER_RE.match(key) else _sanitize_name(key)
        candidate = base
        suffix = 2
        while candidate in used or candidate == _RESERVED_PROPERTY:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        mapping[key] = candidate
    return mapping


def _field_label(name: str, prop: Any) -> str:
    """The human-facing label for a field: its schema ``title`` when a non-empty string, else the property name.

    The one definition both the rendered control label and the completion-payload key
    (:func:`payload_labels`) use, so the key the participant sees in the submitted-form
    summary is always exactly the label shown on the field. A ``title`` that is not a string,
    or is empty or whitespace-only, counts as absent — WhatsApp cannot show a blank field
    label or key — so the property name is used instead.
    """
    title = prop.get("title") if isinstance(prop, dict) else None
    return title if isinstance(title, str) and title.strip() else name


def payload_labels(properties: dict[str, Any]) -> dict[str, str]:
    """Map each schema property name to a unique human-readable completion-payload label.

    The terminal ``complete`` action is keyed by these labels — the label WhatsApp shows on
    each field (its ``title`` when a non-empty string, else the property key; :func:`_field_label`),
    so the submitted-form summary bubble reads in human words. A collision — two properties
    sharing a label, or a label equal to the reserved ``flow_token`` (the correlation token Meta
    injects into every ``nfm_reply``, which the inbound decode strips before the answer reaches
    the door) — is disambiguated deterministically in schema order by appending ``_2``, ``_3`` …
    (the same convention :func:`component_names` uses for names), so the first property to claim a
    label keeps it and no completion key ever clashes with the correlation token. Pure,
    order-preserving and injective: the same property list always yields the same map, so a
    re-send and the inbound decode recompute an identical map from the schema alone and the
    reverse (label → key) is lossless.
    """
    used: set[str] = set()
    mapping: dict[str, str] = {}
    for name, prop in properties.items():
        base = _field_label(name, prop)
        candidate = base
        suffix = 2
        while candidate in used or candidate == _RESERVED_PROPERTY:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        mapping[name] = candidate
    return mapping


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


def _is_array_of_strings(prop: dict[str, Any]) -> bool:
    """Whether a property is an array whose ``items`` are strings — a multiple-choice field."""
    items = prop.get("items")
    return prop.get("type") == "array" and isinstance(items, dict) and items.get("type") == "string"


def _choice_options_enum(prop: dict[str, Any]) -> list[Any] | None:
    """A choice property's schema option list — a string's ``enum`` or an array's ``items.enum`` — else ``None``."""
    enum = prop["items"].get("enum") if _is_array_of_strings(prop) else prop.get("enum")
    return enum if isinstance(enum, list) else None


def _is_choice_field(name: str, prop: dict[str, Any], option_fields: set[str]) -> bool:
    """Whether a property renders as a dynamic CHOICE control (reads a per-send ``data-source``).

    A string that carries a schema ``enum`` or is marked option-bearing by the ask, OR an
    array of strings (multiple choice). Dropdown, RadioButtonsGroup and CheckboxGroup all read
    their options from the per-send ``${data.<field>__ds}`` list.
    """
    if _is_array_of_strings(prop):
        return True
    if prop.get("type") != "string":
        return False
    return prop.get("enum") is not None or name in option_fields


def _choice_component_type(prop: dict[str, Any]) -> str:
    """The Flow control a choice property renders as.

    An array of strings is a ``CheckboxGroup`` (multiple selection). A string choice with a
    schema ``enum`` of at most :data:`RADIO_OPTION_THRESHOLD` options is a ``RadioButtonsGroup``
    (the short-list control); any other string choice — a longer enum, or an option-bearing
    field whose per-send list length is not fixed at publish — stays the dynamic ``Dropdown``.
    """
    if _is_array_of_strings(prop):
        return "CheckboxGroup"
    enum = prop.get("enum")
    if isinstance(enum, list) and len(enum) <= RADIO_OPTION_THRESHOLD:
        return "RadioButtonsGroup"
    return "Dropdown"


def _date_has_constraints(prop: dict[str, Any]) -> bool:
    """Whether a date property carries any platform date constraint (bounds, excluded days, a range pairing)."""
    return any(key in prop for key in _DATE_CONSTRAINT_KEYS)


def _calendar_component(cname: str, label: str, required: bool, prop: dict[str, Any], init: str) -> dict[str, Any]:
    """A single-mode ``CalendarPicker`` for a constrained date field.

    Bounds (``min-date``/``max-date``), excluded explicit dates (``unavailable-dates``) and
    excluded weekdays (dropped from ``include-days``, the days that stay selectable) are baked
    as literals from the schema (they are fixed per published Flow). The control sets and
    returns a ``YYYY-MM-DD`` string, exactly like the bare DatePicker, so a date answer's shape
    is unchanged. A range pairing's span (``minDays``/``maxDays``) is enforced at the single
    answer facet, not drawn here (two independent date controls), so the submitted answer stays
    the two scalar date fields.
    """
    component: dict[str, Any] = {
        "type": "CalendarPicker",
        "name": cname,
        "label": label,
        "mode": "single",
        "required": required,
        "init-value": init,
    }
    if isinstance(prop.get("minDate"), str):
        component["min-date"] = prop["minDate"]
    if isinstance(prop.get("maxDate"), str):
        component["max-date"] = prop["maxDate"]
    unavailable = prop.get("unavailableDates")
    if isinstance(unavailable, list):
        dates = [entry for entry in unavailable if isinstance(entry, str) and _ISO_DATE_RE.match(entry)]
        excluded = {entry.lower() for entry in unavailable if isinstance(entry, str) and entry.lower() in _WEEKDAYS}
        if dates:
            component["unavailable-dates"] = dates
        if excluded:
            component["include-days"] = [_WEEKDAY_CODES[day] for day in _WEEKDAYS if day not in excluded]
    return component


def _checkbox_component(cname: str, label: str, required: bool, prop: dict[str, Any], init: str) -> dict[str, Any]:
    """A ``CheckboxGroup`` for a multiple-choice (array-of-strings) field.

    Reads the per-send option list (``data-source``) and prefill (``init-value``, an array).
    ``min/max-selected-items`` come from the modeled ``minItems``/``maxItems`` bounds; a
    required array with no ``minItems`` floor demands at least one pick (``min-selected-items: 1``).
    """
    component: dict[str, Any] = {
        "type": "CheckboxGroup",
        "name": cname,
        "label": label,
        "required": required,
        "data-source": f"${{data.{cname}__ds}}",
        "init-value": init,
    }
    min_items = prop.get("minItems")
    max_items = prop.get("maxItems")
    if isinstance(min_items, int) and not isinstance(min_items, bool):
        component["min-selected-items"] = min_items
    elif required:
        component["min-selected-items"] = 1
    if isinstance(max_items, int) and not isinstance(max_items, bool):
        component["max-selected-items"] = max_items
    return component


def _dynamic_component(
    name: str, prop: dict[str, Any], required: bool, option_fields: set[str], names: dict[str, str]
) -> dict[str, Any]:
    """One Flow field component for the dynamic (per-send) form.

    The component ``name`` and its data references are the identifier-safe name ``names``
    maps the schema key to; the label keeps the property ``title`` (else the raw property
    name). Every control reads its ``init-value`` from the screen ``data`` model and a
    choice field reads a dynamic ``data-source``, so the send injects the values/options. An
    array of strings renders a CheckboxGroup, a short string enum a RadioButtonsGroup (else a
    Dropdown), and a date field with platform constraints a bounded CalendarPicker. Raises
    ``ChannelInputError`` naming a property outside the supported subset.
    """
    label = _field_label(name, prop)
    cname = names[name]
    prop_type = prop.get("type")
    init = f"${{data.{cname}__init}}"
    if _is_array_of_strings(prop):
        if _choice_options_enum(prop) is None and name not in option_fields:
            raise ChannelInputError(
                f"form property {name!r}: a multiple-choice (array of strings) field needs an 'items' enum "
                "or per-send options to render as a CheckboxGroup"
            )
        return _checkbox_component(cname, label, required, prop, init)
    if _is_choice_field(name, prop, option_fields):
        kind = _choice_component_type(prop)
        return {
            "type": kind,
            "name": cname,
            "label": label,
            "required": required,
            "data-source": f"${{data.{cname}__ds}}",
            "init-value": init,
        }
    if _is_date_field(prop):
        if _date_has_constraints(prop):
            return _calendar_component(cname, label, required, prop, init)
        # A plain (unconstrained) date stays a bare DatePicker: its reference defines no
        # ``required`` parameter (unlike every other input control) and Meta rejects an unknown
        # component property at publish, so the required flag is deliberately not emitted here.
        return {"type": "DatePicker", "name": cname, "label": label, "init-value": init}
    if prop_type == "string":
        return {"type": "TextInput", "name": cname, "label": label, "required": required, "init-value": init}
    if prop_type == "boolean":
        return {"type": "OptIn", "name": cname, "label": label, "required": required, "init-value": init}
    if prop_type in ("integer", "number"):
        return {
            "type": "TextInput",
            "name": cname,
            "label": label,
            "required": required,
            "input-type": "number",
            "init-value": init,
        }
    raise ChannelInputError(
        f"form property {name!r}: unsupported schema type {prop_type!r} — a form field must be "
        "string, string+enum, an array of strings, boolean, integer, or number (no nested objects, "
        "arrays of non-strings, or unions)"
    )


_DS_ITEM_EXAMPLE = [{"id": "a", "title": "a"}]


def _value_type_decl(prop: dict[str, Any]) -> dict[str, Any]:
    """The screen-``data`` type declaration for a field's value.

    Boolean for an OptIn, array for a CheckboxGroup, else a string.
    Used both for a field's ``init-value`` source and for a value collected on an earlier
    screen and forwarded to completion, so the declared type always matches the control.
    """
    prop_type = prop.get("type")
    if prop_type == "boolean":
        return {"type": "boolean", "__example__": False}
    if prop_type == "array":
        return {"type": "array", "items": {"type": "string"}, "__example__": []}
    return {"type": "string", "__example__": ""}


def _field_data_decls(
    name: str, prop: dict[str, Any], option_fields: set[str], names: dict[str, str]
) -> dict[str, Any]:
    """The screen-``data`` declarations a field needs where it is RENDERED.

    Its ``init-value`` source and, for a choice field, its dynamic ``data-source`` —
    keyed by the field's identifier-safe component name.
    """
    cname = names[name]
    decls: dict[str, Any] = {f"{cname}__init": _value_type_decl(prop)}
    if _is_choice_field(name, prop, option_fields):
        decls[f"{cname}__ds"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "title": {"type": "string"}},
            },
            "__example__": _DS_ITEM_EXAMPLE,
        }
    return decls


def _forward_field_data(
    name: str, prop: dict[str, Any], option_fields: set[str], names: dict[str, str]
) -> dict[str, str]:
    """The navigate-payload entries that carry a downstream field's ``init``/``ds`` on to the next screen.

    They enter only at the entry screen, so each step re-forwards the ones its successors
    still need — keyed by the field's identifier-safe component name.
    """
    cname = names[name]
    forwarded = {f"{cname}__init": f"${{data.{cname}__init}}"}
    if _is_choice_field(name, prop, option_fields):
        forwarded[f"{cname}__ds"] = f"${{data.{cname}__ds}}"
    return forwarded


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


def _display_component(block: dict[str, Any], slots: dict[str, str]) -> dict[str, Any]:
    """One Flow display component (TextHeading / TextBody / Image) for a page display block.

    A static block carries its own ``text``/``src``; a slotted block reads ``${data.<slot>}``
    (filled by a reaction while the form is open, empty until then). An image maps to the vendor
    ``Image`` (base64 ``src`` + ``alt-text``), a heading to ``TextHeading``, a body to ``TextBody``.
    """
    kind = block.get("kind")
    slot = block.get("slot")
    if kind == "image":
        src = f"${{data.{slots[slot]}}}" if isinstance(slot, str) else block.get("src")
        component: dict[str, Any] = {"type": "Image", "src": src}
        if isinstance(block.get("alt"), str):
            component["alt-text"] = block["alt"]
        return component
    text = f"${{data.{slots[slot]}}}" if isinstance(slot, str) else block.get("text")
    return {"type": "TextHeading" if kind == "heading" else "TextBody", "text": text}


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


def _cond_literal(value: Any) -> str:
    """One ``visibleWhen`` comparison value as a Flow ``If`` expression literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _visible_when_condition(
    predicate: dict[str, Any], this_fields: list[str], earlier_fields: list[str], names: dict[str, str]
) -> str | None:
    """A Flow ``If`` boolean-expression for a ``visibleWhen`` predicate.

    ``None`` when the controlling field is not readable on this screen (the answer facet still
    enforces visibility regardless).
    """
    field = predicate.get("field")
    if field in this_fields:
        ref = f"${{form.{names[field]}}}"
    elif field in earlier_fields:
        ref = f"${{data.{names[field]}__val}}"
    else:
        return None
    if "equals" in predicate:
        return f"{ref} == {_cond_literal(predicate['equals'])}"
    if "in" in predicate:
        return " || ".join(f"{ref} == {_cond_literal(choice)}" for choice in predicate["in"])
    return f"{ref} != ''"


def _apply_visibility(
    component: dict[str, Any],
    prop: dict[str, Any],
    this_fields: list[str],
    earlier_fields: list[str],
    names: dict[str, str],
) -> dict[str, Any]:
    """Wrap a control in a client-side ``If`` when its property carries a ``visibleWhen`` predicate."""
    predicate = prop.get("visibleWhen")
    if not isinstance(predicate, dict):
        return component
    condition = _visible_when_condition(predicate, this_fields, earlier_fields, names)
    if condition is None:
        return component
    return {"type": "If", "condition": condition, "then": [component]}


def _reaction_event_payload(
    kind: str,
    marker_key: str | None,
    marker_value: str | None,
    this_fields: list[str],
    earlier_fields: list[str],
    names: dict[str, str],
) -> dict[str, str]:
    """A ``data_exchange`` action payload: the event markers plus the values filled so far.

    The event kind rides ``tai42_event``; a field/page trigger its ``tai42_field``/``tai42_page``
    marker. Each known value is keyed by its SCHEMA NAME (this screen's through ``${form.<cname>}``,
    earlier ones through their ``__val`` carrier), so the data endpoint hands the react facet
    partial values keyed exactly as the stored schema. Later (unfilled) fields are omitted.
    """
    payload: dict[str, str] = {_REACTION_EVENT_KEY: kind}
    if marker_key is not None and marker_value is not None:
        payload[marker_key] = marker_value
    for name in this_fields:
        payload[name] = f"${{form.{names[name]}}}"
    for name in earlier_fields:
        payload[name] = f"${{data.{names[name]}__val}}"
    return payload


def _attach_field_reaction(
    component: dict[str, Any], name: str, this_fields: list[str], earlier_fields: list[str], names: dict[str, str]
) -> dict[str, Any]:
    """Attach a ``field_changed`` ``data_exchange`` action to a selectable control, or raise.

    A choice/date control carries the action on ``on-select-action``, an OptIn on
    ``on-click-action``; a plain text or numeric input has no on-change event on WhatsApp and is
    refused loudly (never silently dropped).
    """
    action = {
        "name": "data_exchange",
        "payload": _reaction_event_payload(
            _EVENT_FIELD_CHANGED, _REACTION_FIELD_KEY, name, this_fields, earlier_fields, names
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


def _terminal_footer(this_fields: list[str], earlier_fields: list[str], spec: _RenderSpec) -> dict[str, Any]:
    # The terminal screen's Footer. A reacting form whose submission is reaction-checked fires a
    # ``submitted`` data_exchange (the data endpoint accepts or refuses, then completes); a static
    # form completes directly with the flat union of every field, keyed by human-readable label.
    if spec.submitted:
        payload = _reaction_event_payload(_EVENT_SUBMITTED, None, None, this_fields, earlier_fields, spec.names)
        return {
            "type": "Footer",
            "label": _FOOTER_LABEL,
            "on-click-action": {"name": "data_exchange", "payload": payload},
        }
    payload = {
        spec.labels[name]: (
            f"${{form.{spec.names[name]}}}" if name in this_fields else f"${{data.{spec.names[name]}__val}}"
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
            _EVENT_PAGE_ADVANCED, _REACTION_PAGE_KEY, page_title, this_fields, earlier_fields, spec.names
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
        forward[f"{spec.names[name]}__val"] = f"${{form.{spec.names[name]}}}"
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
    ``data_exchange`` action; a ``visibleWhen`` field is wrapped in a client-side ``If``; a review
    page adds the generic value readback. The footer records any transition in ``routing_model``.
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

    display_blocks = _display_component_list(page, spec.slots)
    components: list[dict[str, Any]] = []
    for name in this_fields:
        component = _dynamic_component(
            name, spec.properties[name], name in spec.required, spec.option_fields, spec.names
        )
        if name in spec.field_changed:
            component = _attach_field_reaction(component, name, this_fields, earlier_fields, spec.names)
        components.append(_apply_visibility(component, spec.properties[name], this_fields, earlier_fields, spec.names))
    readback = _review_readback(spec) if page.get("kind") == "review" else []
    footer = (
        _terminal_footer(this_fields, earlier_fields, spec)
        if is_terminal
        else _step_footer(index, page_title, this_fields, later_fields, earlier_fields, spec, routing_model)
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


def _display_component_list(page: dict[str, Any], slots: dict[str, str]) -> list[dict[str, Any]]:
    """The ordered display components for a page (empty when it carries none)."""
    return [_display_component(block, slots) for block in (page.get("display") or [])]


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
    field is wrapped in a client-side ``If``.

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
