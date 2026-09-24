"""Answer-schema → WhatsApp Flow JSON mapping.

An ``ask`` form ask carries a JSON answer schema; this module renders it as a
publishable WhatsApp Flow the human fills in-chat. The supported subset is a
top-level ``{"type": "object", "properties": {...}, "required": [...]}`` whose
properties map one-to-one onto Flow field components:

* ``string``                 → ``TextInput``
* ``string`` with ``enum``   → ``Dropdown`` (dynamic ``data-source`` items)
* ``boolean``                → ``OptIn``
* ``integer`` / ``number``   → ``TextInput`` with ``input-type: "number"``

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
field, keyed by field name, so the inbound decode reads it exactly as an unpaged
form. A footer reads a control's just-filled value through the unwrapped-component
reference ``${screen.<field>}``; an earlier screen's value rides forward as
``${data.<field>__val}``.

Meta accepts a component ``name``, a screen-``data`` key and a ``${data.…}`` /
``${screen.…}`` reference only in the identifier grammar
``[A-Za-z_][A-Za-z0-9_]*``, while an answer schema may use ANY JSON property name.
So each schema property is first mapped to a unique identifier-safe COMPONENT NAME
(:func:`component_names`), and that component name — never the raw property name — is
the ``<field>`` every component ``name``, data key, reference, navigate-payload key
and completion-payload key above is spelled with; the completion payload's keys are
the component names, so the inbound ``nfm_reply`` decode maps them back to the schema
keys before coercion. Only the human-facing field label keeps the property ``title``
(else the raw property name), which may carry any character.

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
import re
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


def _renders_as_dropdown(name: str, prop: dict[str, Any], option_fields: set[str]) -> bool:
    """Whether a property renders as a choice ``Dropdown`` on the per-send Flow.

    A string that either carries a schema ``enum`` or is marked option-bearing by the ask. Only a string
    maps to a dropdown; a non-string is never one whatever the ask says.
    """
    if prop.get("type") != "string":
        return False
    return prop.get("enum") is not None or name in option_fields


def _dynamic_component(
    name: str, prop: dict[str, Any], required: bool, option_fields: set[str], names: dict[str, str]
) -> dict[str, Any]:
    """One Flow field component for the dynamic (per-send) form.

    The component ``name`` and its data references are the identifier-safe name ``names``
    maps the schema key to; the label keeps the property ``title`` (else the raw property
    name). Every control reads its ``init-value`` from the screen ``data`` model and a
    choice field reads a dynamic ``data-source``, so the send injects the values/options.
    Raises ``ChannelInputError`` naming a property outside the supported subset.
    """
    label = prop.get("title") if isinstance(prop.get("title"), str) else name
    cname = names[name]
    prop_type = prop.get("type")
    init = f"${{data.{cname}__init}}"
    if _renders_as_dropdown(name, prop, option_fields):
        return {
            "type": "Dropdown",
            "name": cname,
            "label": label,
            "required": required,
            "data-source": f"${{data.{cname}__ds}}",
            "init-value": init,
        }
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
        "string, string+enum, boolean, integer, or number (no nested objects, arrays, or unions)"
    )


_DS_ITEM_EXAMPLE = [{"id": "a", "title": "a"}]


def _init_decl(prop: dict[str, Any]) -> dict[str, Any]:
    """The screen-``data`` declaration for a field's ``init-value`` — a boolean for an OptIn, else a string."""
    if prop.get("type") == "boolean":
        return {"type": "boolean", "__example__": False}
    return {"type": "string", "__example__": ""}


def _field_data_decls(
    name: str, prop: dict[str, Any], option_fields: set[str], names: dict[str, str]
) -> dict[str, Any]:
    """The screen-``data`` declarations a field needs where it is RENDERED.

    Its ``init-value`` source and, for a choice field, its dynamic ``data-source`` —
    keyed by the field's identifier-safe component name.
    """
    cname = names[name]
    decls: dict[str, Any] = {f"{cname}__init": _init_decl(prop)}
    if _renders_as_dropdown(name, prop, option_fields):
        decls[f"{cname}__ds"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "title": {"type": "string"}},
            },
            "__example__": _DS_ITEM_EXAMPLE,
        }
    return decls


def _val_decl(prop: dict[str, Any]) -> dict[str, Any]:
    """The screen-``data`` declaration for a value COLLECTED on an earlier screen and forwarded to completion.

    A boolean for an OptIn, else a string.
    """
    if prop.get("type") == "boolean":
        return {"type": "boolean", "__example__": False}
    return {"type": "string", "__example__": ""}


def _forward_field_data(
    name: str, prop: dict[str, Any], option_fields: set[str], names: dict[str, str]
) -> dict[str, str]:
    """The navigate-payload entries that carry a downstream field's ``init``/``ds`` on to the next screen.

    They enter only at the entry screen, so each step re-forwards the ones its successors
    still need — keyed by the field's identifier-safe component name.
    """
    cname = names[name]
    forwarded = {f"{cname}__init": f"${{data.{cname}__init}}"}
    if _renders_as_dropdown(name, prop, option_fields):
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


def _screen_data_model(
    index: int,
    this_fields: list[str],
    later_fields: list[str],
    earlier_fields: list[str],
    properties: dict[str, Any],
    option_fields: set[str],
    names: dict[str, str],
) -> dict[str, Any]:
    """The screen's ``data`` declarations.

    The entry screen declares EVERY field's init/ds (the send injects them all there); a later screen
    declares its own and its successors' init/ds plus a ``__val`` carrier for each field collected earlier.
    """
    data_model: dict[str, Any] = {}
    if index == 0:
        for name, prop in properties.items():
            data_model.update(_field_data_decls(name, prop, option_fields, names))
    else:
        for name in [*this_fields, *later_fields]:
            data_model.update(_field_data_decls(name, properties[name], option_fields, names))
        for name in earlier_fields:
            data_model[f"{names[name]}__val"] = _val_decl(properties[name])
    return data_model


def _screen_footer(
    index: int,
    is_terminal: bool,
    this_fields: list[str],
    later_fields: list[str],
    earlier_fields: list[str],
    properties: dict[str, Any],
    option_fields: set[str],
    routing_model: dict[str, list[str]],
    names: dict[str, str],
) -> dict[str, Any]:
    """The screen's ``Footer`` component.

    The terminal screen completes with the flat union of every field (this screen's read through the
    unwrapped-component reference ``${screen.<field>}``, earlier ones from their ``__val`` carriers); a
    non-terminal screen navigates to the next, forwarding successors' init/ds and every collected value,
    and records the transition in ``routing_model``. Every payload key and reference is the field's
    identifier-safe component name — the completion payload keys are what the inbound reply carries back.
    """
    if is_terminal:
        payload = {
            names[name]: (f"${{screen.{names[name]}}}" if name in this_fields else f"${{data.{names[name]}__val}}")
            for name in properties
        }
        return {"type": "Footer", "label": _FOOTER_LABEL, "on-click-action": {"name": "complete", "payload": payload}}
    screen_id = _screen_id(index)
    next_screen = _screen_id(index + 1)
    routing_model[screen_id] = [next_screen]
    forward: dict[str, str] = {}
    for name in later_fields:
        forward.update(_forward_field_data(name, properties[name], option_fields, names))
    for name in earlier_fields:
        forward[f"{names[name]}__val"] = f"${{data.{names[name]}__val}}"
    for name in this_fields:
        forward[f"{names[name]}__val"] = f"${{screen.{names[name]}}}"
    return {
        "type": "Footer",
        "label": _CONTINUE_LABEL,
        "on-click-action": {
            "name": "navigate",
            "next": {"type": "screen", "name": next_screen},
            "payload": forward,
        },
    }


def _build_form_screen(
    index: int,
    page: dict[str, Any],
    properties: dict[str, Any],
    required: set[str],
    option_fields: set[str],
    fields_by_screen: list[list[str]],
    earlier_fields: list[str],
    screen_count: int,
    routing_model: dict[str, list[str]],
    names: dict[str, str],
) -> dict[str, Any]:
    """One Flow screen for a page: its field components, its ``data`` model, and its footer.

    The footer records any transition in ``routing_model``.
    """
    this_fields = fields_by_screen[index]
    later_fields = [field for screen in fields_by_screen[index + 1 :] for field in screen]
    is_terminal = index == screen_count - 1

    data_model = _screen_data_model(index, this_fields, later_fields, earlier_fields, properties, option_fields, names)
    components = [
        _dynamic_component(name, properties[name], name in required, option_fields, names) for name in this_fields
    ]
    footer = _screen_footer(
        index, is_terminal, this_fields, later_fields, earlier_fields, properties, option_fields, routing_model, names
    )

    screen: dict[str, Any] = {
        "id": _screen_id(index),
        "title": str(page["title"]),
        "terminal": is_terminal,
        "layout": {
            "type": "SingleColumnLayout",
            "children": [*components, footer],
        },
    }
    if data_model:
        screen["data"] = data_model
    return screen


def build_form_flow(
    schema: dict[str, Any],
    pages: list[dict[str, Any]] | None = None,
    option_fields: set[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """The ``(flow_json, key)`` for a per-send/stepped form ask — publishable Flow JSON.

    One screen per page (``pages`` absent → one screen carrying every property in
    schema order); each screen holds its field components and footer directly (no
    ``Form`` wrapper) and its ``id`` is letters-and-underscores only. Each schema property
    is mapped to a unique identifier-safe component name (:func:`component_names`), which
    is what every component ``name``, data key, reference and completion-payload key uses,
    so a property named with any character still yields a publishable Flow. Each choice field
    reads a dynamic ``data-source`` and every control an ``init-value``, so the send
    supplies the values/options through ``flow_action_payload.data``. A string property
    renders as a choice ``Dropdown`` when it carries a schema ``enum`` or when
    ``option_fields`` (the ask's option-bearing set, the ``data.options`` keys) names it;
    any other string stays a ``TextInput``. Collected values thread forward across
    screens and the terminal screen completes with the flat union of every field.
    ``key`` is the hash of the ``(schema, pages, option_fields)`` and the emitted
    ``flow_json``. Pure — no I/O. Raises ``ChannelInputError`` naming any property
    outside the subset or any page field that is not a declared property.
    """
    option_fields = option_fields or set()
    properties, required = _validate_object_schema(schema)
    names = component_names(properties)
    _validate_form_properties(properties, required, option_fields, names)
    resolved_pages, fields_by_screen = _resolve_pages(properties, pages)

    screen_count = len(resolved_pages)
    screens: list[dict[str, Any]] = []
    routing_model: dict[str, list[str]] = {}
    earlier_fields: list[str] = []
    for index, page in enumerate(resolved_pages):
        screens.append(
            _build_form_screen(
                index,
                page,
                properties,
                required,
                option_fields,
                fields_by_screen,
                earlier_fields,
                screen_count,
                routing_model,
                names,
            )
        )
        earlier_fields = [*earlier_fields, *fields_by_screen[index]]

    flow_json: dict[str, Any] = {"version": _FLOW_JSON_VERSION, "screens": screens}
    if screen_count > 1:
        # A multi-screen navigate flow declares its allowed transitions.
        routing_model[_screen_id(screen_count - 1)] = []
        flow_json["routing_model"] = routing_model
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
    marks option-bearing (a key in ``options``) — matching the published Flow's dynamic
    dropdowns. A per-send option list keyed on a NON-STRING property cannot be honored
    (only a string maps to a dropdown) and is refused loudly, naming the field. Raises
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
        if not isinstance(prop, dict) or prop.get("type") != "string":
            raise ChannelInputError(
                f"form field {name!r}: WhatsApp cannot apply per-send options to a non-string property "
                "— only a string property maps to a dropdown"
            )

    option_fields = set(options)
    data: dict[str, Any] = {}
    for name, prop in properties.items():
        cname = names[name]
        prop_type = prop.get("type") if isinstance(prop, dict) else None
        if name in values:
            raw = values[name]
            data[f"{cname}__init"] = bool(raw) if prop_type == "boolean" else raw if isinstance(raw, str) else str(raw)
        else:
            data[f"{cname}__init"] = False if prop_type == "boolean" else ""
        if isinstance(prop, dict) and _renders_as_dropdown(name, prop, option_fields):
            if name in options:
                data[f"{cname}__ds"] = [
                    {"id": choice["value"], "title": choice.get("label") or choice["value"]} for choice in options[name]
                ]
            else:
                data[f"{cname}__ds"] = [{"id": str(item), "title": str(item)} for item in prop["enum"]]
    return data
