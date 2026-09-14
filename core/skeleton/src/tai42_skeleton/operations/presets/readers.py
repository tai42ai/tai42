"""Structural body readers that raise the byte-stable 400 on malformed nested
combo / output-schema / state-binding payloads before any store write."""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter, ValidationError
from tai42_contract.manifest import ExtensionElement
from tai42_contract.states.binding import StateBinding
from tai42_contract.template import TemplatedText

from tai42_skeleton.operations import BadRequestError


def read_element(element: Any) -> ExtensionElement:
    """One combo element, structurally validated: a non-empty extension NAME
    (bare string), or a ``{"name": <non-empty str>, "config": <dict>}`` mapping
    binding author config (``config`` REQUIRED — a config-less selection is the
    bare-string form, so a config-free dict is malformed) with no other keys.
    Anything else is a loud 400. Registration of the name is checked later against
    the live registry."""
    if isinstance(element, str):
        if not element:
            raise BadRequestError("an extension name must be a non-empty string")
        return element
    if isinstance(element, dict):
        name = element.get("name")
        if not isinstance(name, str) or not name:
            raise BadRequestError("an extension element must have a non-empty string 'name'")
        config = element.get("config")
        if not isinstance(config, dict):
            raise BadRequestError(f"extension element {name!r} must carry a 'config' mapping")
        extra = set(element) - {"name", "config"}
        if extra:
            raise BadRequestError(f"extension element {name!r} has unexpected keys: {sorted(extra)!r}")
        return {"name": name, "config": dict(config)}
    raise BadRequestError("each combo element must be an extension name or a {'name', 'config'} mapping")


def read_combos(extensions: Any) -> list[list[ExtensionElement]]:
    """A list of extension combos, each a non-empty list of combo elements. The
    empty INNER combo (``[[]]`` or any ``[]`` member) is rejected — mirrors the
    view's rule so a create/edit is guarded before any store write."""
    result: list[list[ExtensionElement]] = []
    for combo in extensions:
        if not isinstance(combo, list) or not combo:
            raise BadRequestError("each extension combo must be a non-empty list of extension elements")
        result.append([read_element(element) for element in combo])
    return result


def read_create_extensions(present: bool, value: Any) -> list[list[ExtensionElement]]:
    """Create's extension combos: an absent field means no extensions, an explicit
    ``extensions: []`` is REJECTED (nothing to clear on create), and an empty inner
    combo is rejected."""
    if not present:
        return []
    if not isinstance(value, list):
        raise BadRequestError("'extensions' must be a list of combos")
    if value == []:
        raise BadRequestError("explicit empty 'extensions' is rejected; omit the field for no extensions")
    return read_combos(value)


def read_edit_extensions(present: bool, value: Any) -> list[list[ExtensionElement]] | None:
    """Save-version's extension combos under the carry-forward sentinel: absent or
    ``null`` carries forward (``None``); ``[]`` clears; an empty inner combo is
    rejected."""
    if not present or value is None:
        return None
    if not isinstance(value, list):
        raise BadRequestError("'extensions' must be a list of combos")
    return read_combos(value)


def _read_schema_body(field: str, value: Any) -> TemplatedText | dict[str, Any] | None:
    """The optional author-set schema body — the ``TemplatedText | dict`` union — from a
    request value: ``null`` → ``None``; a ``{content|id, kwargs}`` object → the parsed
    :class:`~tai42_contract.template.TemplatedText` (a stored schema named by id, or inline
    templated content); any other JSON object → the inline schema dict itself; anything else
    → a loud 400. A by-id resource is not fetched here — it is rendered, parsed and validated
    at the save-time dry-run bake and at the point of use."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BadRequestError(f"'{field}' must be a JSON object (a JSON Schema) or a stored-body reference")
    try:
        return TypeAdapter(TemplatedText | dict[str, Any]).validate_python(value)
    except ValidationError as exc:
        raise BadRequestError(f"invalid '{field}': {exc.errors(include_url=False)}") from exc


def read_output_schema(value: Any) -> TemplatedText | dict[str, Any] | None:
    """The optional author-set output schema from a request value (see :func:`_read_schema_body`)."""
    return _read_schema_body("output_schema", value)


def read_input_schema(value: Any) -> TemplatedText | dict[str, Any] | None:
    """The optional author-set input schema from a request value (see :func:`_read_schema_body`)."""
    return _read_schema_body("input_schema", value)


def read_state_binding(value: Any) -> StateBinding | None:
    """The optional door-layer state binding from a request value: ``null`` → ``None``; a
    binding object → the parsed :class:`StateBinding`; a malformed shape → a loud 400. The
    HTTP-edge extractor uses this so a body binding is not silently dropped before the op."""
    if value is None:
        return None
    try:
        return StateBinding.model_validate(value)
    except ValidationError as exc:
        raise BadRequestError(f"invalid 'state_binding': {exc.errors(include_url=False)}") from exc
