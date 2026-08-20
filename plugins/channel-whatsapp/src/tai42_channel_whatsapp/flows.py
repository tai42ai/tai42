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
# The Form component name the field bindings (``${form.<field>}``) resolve against.
_FORM_NAME = "form"
# Generic labels — a form ask carries no domain-specific chrome.
_SCREEN_TITLE = "Form"
_FOOTER_LABEL = "Submit"

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
