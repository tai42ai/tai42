"""The channel-deliverable form-schema subset — the ONE definition of the SHARED
subset every ``form`` question delivered over a channel must satisfy.

A channel form is answered on the server-rendered callback page, a flat HTML form
the human fills and submits. Only a schema that renders into such a form is
allowed: root ``{"type": "object"}`` with a non-empty ``properties`` map; every
property a scalar (``string``/``boolean``/``integer``/``number``); ``enum`` only
on a ``string`` property, a non-empty list of strings; a ``required`` list, when
present, naming only declared properties. A property WITHOUT a scalar ``type`` — a
nested object, an array, a bare ``anyOf``/``oneOf``/``$ref``, a missing type —
cannot be rendered into a control and is refused, naming the offending property. An
extra constraint keyword riding alongside a scalar ``type`` (``pattern``,
``minimum``, an inline ``anyOf``, ...) is not rendered but is enforced when the
answer is validated.

The ask-time guard (``ask_user``) and the callback form renderer share this ONE
walk, so the shared subset is judged by a single rule set. Channel-SPECIFIC limits
beyond this subset (reserved property names, a medium's own Block Kit / Flow caps)
are NOT defined here — the named channel's ``validate_form_schema`` hook enforces
them at ask-time on top of this walk.
"""

from __future__ import annotations

from typing import Any

_SCALAR_TYPES = ("string", "boolean", "integer", "number")


def _validate_property(name: str, prop: Any) -> None:
    if not isinstance(prop, dict):
        raise ValueError(f"form schema property {name!r} must be an object")
    ptype = prop.get("type")
    if ptype not in _SCALAR_TYPES:
        raise ValueError(
            f"form schema property {name!r} has type {ptype!r}; a channel form allows only a scalar "
            f"type ({', '.join(_SCALAR_TYPES)}) — a property without one (nested object, array, bare "
            f"anyOf/oneOf/$ref, missing type) cannot be rendered into a control"
        )
    enum = prop.get("enum")
    if enum is None:
        return
    if ptype != "string":
        raise ValueError(f"form schema property {name!r} has an enum but is not a 'string' property")
    if not isinstance(enum, list) or not enum:
        raise ValueError(f"form schema property {name!r} enum must be a non-empty list")
    if not all(isinstance(choice, str) for choice in enum):
        raise ValueError(f"form schema property {name!r} enum must contain only strings")


def channel_form_fields(schema: Any) -> list[tuple[str, dict, bool]]:
    """Validate ``schema`` against the channel-deliverable form subset and return
    its fields as ``(name, prop, is_required)`` in declared order.

    Raises ``ValueError`` naming the offending property (or the violated root
    rule) on any deviation from the subset."""
    if not isinstance(schema, dict):
        raise ValueError("form schema must be an object")
    if schema.get("type") != "object":
        raise ValueError(f"form schema top-level type must be 'object', got {schema.get('type')!r}")
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise ValueError("form schema must carry a non-empty object 'properties' map")
    required = schema.get("required", [])
    if not isinstance(required, list):
        raise ValueError("form schema 'required' must be a list when present")
    undeclared = [name for name in required if name not in properties]
    if undeclared:
        raise ValueError(f"form schema 'required' names undeclared properties: {undeclared}")
    required_set = set(required)
    fields: list[tuple[str, dict, bool]] = []
    for name, prop in properties.items():
        key = str(name)
        _validate_property(key, prop)
        fields.append((key, prop, name in required_set))
    return fields


def validate_channel_form_schema(schema: dict) -> None:
    """Raise ``ValueError`` (naming the offending property and rule) when ``schema``
    falls outside the channel-deliverable form subset; return ``None`` when it
    conforms. Callers pass a JSON-schema dict — normalize a pydantic model first."""
    channel_form_fields(schema)


def effective_answer_schema(schema: dict[str, Any], data: Any) -> dict[str, Any]:
    """Return ``schema`` with each per-send option list applied as its property's
    ``enum`` for ANSWER validation: a per-send list replaces the published ``enum``
    for one send, so a submitted value is judged against the choices the human was
    actually shown. ``data`` is the stored :class:`FormData` dump (or ``None``);
    absent options return ``schema`` unchanged. Non-mutating (shallow copies only)."""
    options = (data or {}).get("options") if isinstance(data, dict) else None
    if not options:
        return schema
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return schema
    new_properties = dict(properties)
    for name, option_list in options.items():
        prop = new_properties.get(name)
        if not isinstance(prop, dict):
            continue
        enum = [option["value"] for option in option_list]
        if prop.get("type") == "array":
            # An array property carries its choices on ``items``, not the array itself:
            # placing the ``enum`` on the array would demand the whole submitted list
            # equal one option, rejecting every real multi-select answer.
            items = prop.get("items")
            items = items if isinstance(items, dict) else {}
            new_properties[name] = {**prop, "items": {**items, "enum": enum}}
        else:
            new_properties[name] = {**prop, "enum": enum}
    return {**schema, "properties": new_properties}
