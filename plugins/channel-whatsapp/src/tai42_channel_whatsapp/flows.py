"""Answer-schema → WhatsApp Flow JSON mapping.

An ``ask_user`` form ask carries a JSON answer schema; this module renders it as
a single-screen WhatsApp Flow the human fills in-chat. The supported subset is a
top-level ``{"type": "object", "properties": {...}, "required": [...]}`` whose
properties map one-to-one onto Flow field components:

* ``string``                 → ``TextInput``
* ``string`` with ``enum``   → ``Dropdown`` (static ``data-source`` items)
* ``boolean``                → ``OptIn``
* ``integer`` / ``number``   → ``TextInput`` with ``input-type: "number"``

Each property's ``title`` (else the property name) is the field label; the
``required`` list flags the components. Anything outside the subset — a nested
object, an array, a ``oneOf``/``anyOf``, an unknown type, or the reserved
``flow_token`` property name — is a permanent input refusal (the medium cannot
render it BY NATURE): it raises ``ChannelInputError`` naming the property and why,
before any network work, and is never retried.

The emitted Flow is one terminal screen ``"FORM"`` with a ``SingleColumnLayout``
holding one ``Form`` named ``"form"``; its ``Footer`` completes the flow with a
payload binding every field to ``${form.<field>}``. ``build_flow`` is pure and
also returns the canonical schema hash (sha256 over a sorted-keys, compact JSON
dump) that keys the published-Flow cache.

``build_form_flow`` is the ASK (deliver) variant that carries the inline feature's
per-send data and pages. It publishes ONE Flow per ``(schema, pages, option_fields)``
triple (never per send): each page becomes its own screen (``SCREEN_0`` the entry,
the last terminal), a choice field's ``Dropdown`` reads a DYNAMIC ``data-source`` and
every control an ``init-value`` from the screen's ``data`` model, so the send injects
the prefilled values and the per-send option lists through ``flow_action_payload.data``
(built by :func:`build_flow_data`) rather than re-publishing. Collected values are
threaded forward across screens by each step's navigate payload; the terminal
screen completes with the flat UNION of every field, keyed by field name, so the
inbound decode reads it exactly as an unpaged form.

A string property renders as a dynamic ``Dropdown`` when it carries a schema ``enum``
OR when the ask marks it option-bearing (``option_fields`` — the set of
``flow_action_payload.data.options`` keys); this matches the web and Slack channels,
which honor per-send options on ANY string property. Because that set decides which
Flow is published, it joins the publish key: a schema reused with a different
option-bearing set publishes its own Flow, while an unchanged triple reuses one. A
string property that is neither enum nor option-bearing stays a ``TextInput``. A
per-send option list on a NON-STRING property cannot be honored (only a string maps
to a dropdown) and is refused loudly, naming the field.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from tai42_contract.channels import ChannelInputError

# Flow JSON version pinned to a Cloud-API-valid release. This is a static
# (endpoint-less) navigate flow whose single terminal screen completes with the
# form payload — no ``data_api_version`` because there is no data-exchange
# endpoint. Bump this constant when a newer schema version is adopted.
_FLOW_JSON_VERSION = "7.0"

# The single terminal screen id; the send's flow_action_payload navigates to it.
_SCREEN_ID = "FORM"
# The per-page screen id prefix for a stepped form (``SCREEN_0`` is the entry screen
# the send navigates to). One screen per page; the last is terminal.
_SCREEN_PREFIX = "SCREEN_"
# The Form component name the field bindings (``${form.<field>}``) resolve against.
_FORM_NAME = "form"
# Generic labels — a form ask carries no domain-specific chrome.
_SCREEN_TITLE = "Form"
_FOOTER_LABEL = "Submit"
# The non-terminal step's footer — advances to the next screen.
_CONTINUE_LABEL = "Continue"

# Meta injects ``flow_token`` into every Flow response to correlate the reply, so a
# form property of that name is unanswerable on this channel: the reply handler
# strips the key before the answer reaches the door. The mapper refuses it up front.
_RESERVED_PROPERTY = "flow_token"


def _field_component(name: str, prop: dict[str, Any], required: bool) -> dict[str, Any]:
    """One Flow field component for a single top-level schema property, or raise
    ``ChannelInputError`` naming the property when it is outside the subset."""
    label = prop.get("title") if isinstance(prop.get("title"), str) else name
    prop_type = prop.get("type")
    enum = prop.get("enum")

    if prop_type == "string" and enum is not None:
        if not isinstance(enum, list) or not enum or not all(isinstance(item, str) for item in enum):
            raise ChannelInputError(f"form property {name!r}: a string enum must be a non-empty list of strings")
        return {
            "type": "Dropdown",
            "name": name,
            "label": label,
            "required": required,
            "data-source": [{"id": item, "title": item} for item in enum],
        }
    if prop_type == "string":
        return {"type": "TextInput", "name": name, "label": label, "required": required}
    if prop_type == "boolean":
        return {"type": "OptIn", "name": name, "label": label, "required": required}
    if prop_type in ("integer", "number"):
        return {
            "type": "TextInput",
            "name": name,
            "label": label,
            "required": required,
            "input-type": "number",
        }
    raise ChannelInputError(
        f"form property {name!r}: unsupported schema type {prop_type!r} — a form field must be "
        "string, string+enum, boolean, integer, or number (no nested objects, arrays, or unions)"
    )


def _canonical_hash(schema: dict[str, Any]) -> str:
    """sha256 hex over a canonical (sorted-keys, compact-separator) JSON dump."""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_flow(schema: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The ``(flow_json, schema_hash)`` for a form answer schema.

    Validates the schema is the supported ``object`` subset and maps each property
    to a field component; raises ``ChannelInputError`` (naming the offending
    property or shape) on anything outside it. Pure — no I/O.
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
    required = set(required_raw)

    components: list[dict[str, Any]] = []
    payload: dict[str, str] = {}
    for name, prop in properties.items():
        if name == _RESERVED_PROPERTY:
            raise ChannelInputError(
                f"form property {name!r}: reserved on this channel — Meta injects {name!r} into the "
                "Flow response to correlate the reply, so a field of that name is unanswerable"
            )
        if not isinstance(prop, dict):
            raise ChannelInputError(f"form property {name!r}: schema must be an object")
        components.append(_field_component(name, prop, name in required))
        payload[name] = f"${{form.{name}}}"

    components.append(
        {"type": "Footer", "label": _FOOTER_LABEL, "on-click-action": {"name": "complete", "payload": payload}}
    )
    flow_json = {
        "version": _FLOW_JSON_VERSION,
        "screens": [
            {
                "id": _SCREEN_ID,
                "title": _SCREEN_TITLE,
                "terminal": True,
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [{"type": "Form", "name": _FORM_NAME, "children": components}],
                },
            }
        ],
    }
    return flow_json, _canonical_hash(schema)


def _canonical_hash_pages(schema: dict[str, Any], pages: list[dict[str, Any]], option_fields: set[str]) -> str:
    """sha256 hex over a canonical dump of the ``(schema, pages, option_fields)`` TRIPLE —
    the published Flow key for a stepped/per-send form. A different page layout OR a
    different option-bearing set keys a different published Flow, and the ``pages``/
    ``option_fields`` members keep it from colliding with the ask-less (schema-only) hash."""
    canonical = json.dumps(
        {"schema": schema, "pages": pages, "option_fields": sorted(option_fields)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _renders_as_dropdown(name: str, prop: dict[str, Any], option_fields: set[str]) -> bool:
    """Whether a property renders as a choice ``Dropdown`` on the per-send Flow — a
    string that either carries a schema ``enum`` or is marked option-bearing by the ask.
    Only a string maps to a dropdown; a non-string is never one whatever the ask says."""
    if prop.get("type") != "string":
        return False
    return prop.get("enum") is not None or name in option_fields


def _dynamic_component(name: str, prop: dict[str, Any], required: bool, option_fields: set[str]) -> dict[str, Any]:
    """One Flow field component for the dynamic (per-send) form: every control reads
    its ``init-value`` from the screen ``data`` model and a choice field reads a dynamic
    ``data-source``, so the send injects the values/options. Raises ``ChannelInputError``
    naming a property outside the supported subset."""
    label = prop.get("title") if isinstance(prop.get("title"), str) else name
    prop_type = prop.get("type")
    init = f"${{data.{name}__init}}"
    if _renders_as_dropdown(name, prop, option_fields):
        return {
            "type": "Dropdown",
            "name": name,
            "label": label,
            "required": required,
            "data-source": f"${{data.{name}__ds}}",
            "init-value": init,
        }
    if prop_type == "string":
        return {"type": "TextInput", "name": name, "label": label, "required": required, "init-value": init}
    if prop_type == "boolean":
        return {"type": "OptIn", "name": name, "label": label, "required": required, "init-value": init}
    if prop_type in ("integer", "number"):
        return {
            "type": "TextInput",
            "name": name,
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
    """The screen-``data`` declaration for a field's ``init-value`` — a boolean for an
    OptIn, else a string (a text/number input or a dropdown selection id)."""
    if prop.get("type") == "boolean":
        return {"type": "boolean", "__example__": False}
    return {"type": "string", "__example__": ""}


def _field_data_decls(name: str, prop: dict[str, Any], option_fields: set[str]) -> dict[str, Any]:
    """The screen-``data`` declarations a field needs where it is RENDERED: its
    ``init-value`` source and, for a choice field, its dynamic ``data-source``."""
    decls: dict[str, Any] = {f"{name}__init": _init_decl(prop)}
    if _renders_as_dropdown(name, prop, option_fields):
        decls[f"{name}__ds"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "title": {"type": "string"}},
            },
            "__example__": _DS_ITEM_EXAMPLE,
        }
    return decls


def _val_decl(prop: dict[str, Any]) -> dict[str, Any]:
    """The screen-``data`` declaration for a value COLLECTED on an earlier screen and
    forwarded to the terminal completion — a boolean for an OptIn, else a string."""
    if prop.get("type") == "boolean":
        return {"type": "boolean", "__example__": False}
    return {"type": "string", "__example__": ""}


def _forward_field_data(name: str, prop: dict[str, Any], option_fields: set[str]) -> dict[str, str]:
    """The navigate-payload entries that carry a downstream field's ``init``/``ds`` on
    to the next screen (they enter only at the entry screen, so each step re-forwards
    the ones its successors still need)."""
    forwarded = {f"{name}__init": f"${{data.{name}__init}}"}
    if _renders_as_dropdown(name, prop, option_fields):
        forwarded[f"{name}__ds"] = f"${{data.{name}__ds}}"
    return forwarded


def build_form_flow(
    schema: dict[str, Any],
    pages: list[dict[str, Any]] | None = None,
    option_fields: set[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """The ``(flow_json, key)`` for a per-send/stepped form ask.

    One screen per page (``pages`` absent → one screen carrying every property in
    schema order); each choice field reads a dynamic ``data-source`` and every control
    an ``init-value``, so the send supplies the values/options through
    ``flow_action_payload.data``. A string property renders as a choice ``Dropdown`` when
    it carries a schema ``enum`` or when ``option_fields`` (the ask's option-bearing set,
    the ``data.options`` keys) names it; any other string stays a ``TextInput``. Collected
    values thread forward across screens and the terminal screen completes with the flat
    union of every field. ``key`` is the hash of the ``(schema, pages, option_fields)``
    triple. Pure — no I/O. Raises ``ChannelInputError`` naming any property outside the
    subset or any page field that is not a declared property.
    """
    option_fields = option_fields or set()
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
    required = set(required_raw)

    for name, prop in properties.items():
        if name == _RESERVED_PROPERTY:
            raise ChannelInputError(
                f"form property {name!r}: reserved on this channel — Meta injects {name!r} into the "
                "Flow response to correlate the reply, so a field of that name is unanswerable"
            )
        if not isinstance(prop, dict):
            raise ChannelInputError(f"form property {name!r}: schema must be an object")
        # Validate the subset up front (raises naming the property on an unsupported type).
        _dynamic_component(name, prop, name in required, option_fields)

    resolved_pages = pages if pages else [{"title": _SCREEN_TITLE, "fields": list(properties)}]
    fields_by_screen: list[list[str]] = []
    for page in resolved_pages:
        page_fields = [str(field) for field in page["fields"]]
        for field in page_fields:
            if field not in properties:
                raise ChannelInputError(f"form page names unknown property {field!r}")
        fields_by_screen.append(page_fields)

    screen_count = len(resolved_pages)
    screens: list[dict[str, Any]] = []
    routing_model: dict[str, list[str]] = {}
    earlier_fields: list[str] = []
    for index, page in enumerate(resolved_pages):
        screen_id = f"{_SCREEN_PREFIX}{index}"
        this_fields = fields_by_screen[index]
        later_fields = [field for screen in fields_by_screen[index + 1 :] for field in screen]
        is_terminal = index == screen_count - 1

        data_model: dict[str, Any] = {}
        if index == 0:
            # The entry screen declares EVERY field's init/ds — the send injects them all
            # here and each step forwards the ones its successors still need.
            for name, prop in properties.items():
                data_model.update(_field_data_decls(name, prop, option_fields))
        else:
            for name in [*this_fields, *later_fields]:
                data_model.update(_field_data_decls(name, properties[name], option_fields))
            for name in earlier_fields:
                data_model[f"{name}__val"] = _val_decl(properties[name])

        components = [
            _dynamic_component(name, properties[name], name in required, option_fields) for name in this_fields
        ]

        if is_terminal:
            payload = {
                name: (f"${{form.{name}}}" if name in this_fields else f"${{data.{name}__val}}") for name in properties
            }
            footer = {
                "type": "Footer",
                "label": _FOOTER_LABEL,
                "on-click-action": {"name": "complete", "payload": payload},
            }
        else:
            next_screen = f"{_SCREEN_PREFIX}{index + 1}"
            routing_model[screen_id] = [next_screen]
            forward: dict[str, str] = {}
            for name in later_fields:
                forward.update(_forward_field_data(name, properties[name], option_fields))
            for name in earlier_fields:
                forward[f"{name}__val"] = f"${{data.{name}__val}}"
            for name in this_fields:
                forward[f"{name}__val"] = f"${{form.{name}}}"
            footer = {
                "type": "Footer",
                "label": _CONTINUE_LABEL,
                "on-click-action": {
                    "name": "navigate",
                    "next": {"type": "screen", "name": next_screen},
                    "payload": forward,
                },
            }

        screen: dict[str, Any] = {
            "id": screen_id,
            "title": str(page["title"]),
            "terminal": is_terminal,
            "layout": {
                "type": "SingleColumnLayout",
                "children": [{"type": "Form", "name": _FORM_NAME, "children": [*components, footer]}],
            },
        }
        if data_model:
            screen["data"] = data_model
        screens.append(screen)
        earlier_fields = [*earlier_fields, *this_fields]

    flow_json: dict[str, Any] = {"version": _FLOW_JSON_VERSION, "screens": screens}
    if screen_count > 1:
        # A multi-screen navigate flow declares its allowed transitions.
        routing_model[f"{_SCREEN_PREFIX}{screen_count - 1}"] = []
        flow_json["routing_model"] = routing_model
    return flow_json, _canonical_hash_pages(schema, resolved_pages, option_fields)


def build_flow_data(
    schema: dict[str, Any],
    values: dict[str, Any] | None,
    options: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    """The ``flow_action_payload.data`` a per-send form carries: every field's
    ``init`` (its prefilled value, or the empty default) and every choice field's ``ds``
    (its per-send option list ``{id, title}``, or the schema ``enum`` as the default).

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
        prop_type = prop.get("type") if isinstance(prop, dict) else None
        if name in values:
            raw = values[name]
            data[f"{name}__init"] = bool(raw) if prop_type == "boolean" else raw if isinstance(raw, str) else str(raw)
        else:
            data[f"{name}__init"] = False if prop_type == "boolean" else ""
        if isinstance(prop, dict) and _renders_as_dropdown(name, prop, option_fields):
            if name in options:
                data[f"{name}__ds"] = [
                    {"id": choice["value"], "title": choice.get("label") or choice["value"]} for choice in options[name]
                ]
            else:
                data[f"{name}__ds"] = [{"id": str(item), "title": str(item)} for item in prop["enum"]]
    return data
