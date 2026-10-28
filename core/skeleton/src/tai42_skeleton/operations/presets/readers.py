"""Structural body readers that raise the byte-stable 400 before any store write.

They reject malformed nested combo / schema / map / state-binding payloads.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter, ValidationError
from tai42_contract.manifest import ExtensionElement, normalize_extension_element
from tai42_contract.states.binding import StateBinding
from tai42_contract.template import TemplatedText

from tai42_skeleton.operations import BadRequestError


def read_element(element: Any) -> ExtensionElement:
    """One combo element, structurally validated by the contract's normalizer; a malformed one is a loud 400.

    Registration of the name is checked later against the live registry.
    """
    try:
        return normalize_extension_element(element)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def read_combos(extensions: Any) -> list[list[ExtensionElement]]:
    """A list of extension combos, each a non-empty list of combo elements.

    The empty INNER combo (``[[]]`` or any ``[]`` member) is rejected — mirrors the view's rule so
    a create/edit is guarded before any store write.
    """
    result: list[list[ExtensionElement]] = []
    for combo in extensions:
        if not isinstance(combo, list) or not combo:
            raise BadRequestError("each extension combo must be a non-empty list of extension elements")
        result.append([read_element(element) for element in combo])
    return result


def read_create_extensions(present: bool, value: Any) -> list[list[ExtensionElement]]:
    """Create's extension combos.

    An absent field means no extensions, an explicit ``extensions: []`` is REJECTED (nothing to
    clear on create), and an empty inner combo is rejected.
    """
    if not present:
        return []
    if not isinstance(value, list):
        raise BadRequestError("'extensions' must be a list of combos")
    if value == []:
        raise BadRequestError("explicit empty 'extensions' is rejected; omit the field for no extensions")
    return read_combos(value)


def read_edit_extensions(present: bool, value: Any) -> list[list[ExtensionElement]] | None:
    """Save-version's extension combos under the carry-forward sentinel.

    Absent or ``null`` carries forward (``None``); ``[]`` clears; an empty inner combo is rejected.
    """
    if not present or value is None:
        return None
    if not isinstance(value, list):
        raise BadRequestError("'extensions' must be a list of combos")
    return read_combos(value)


def _read_schema_body(field: str, value: Any) -> TemplatedText | dict[str, Any] | None:
    """The optional author-set schema body — the ``TemplatedText | dict`` union — from a request value.

    ``null`` → ``None``; a ``{content|id, kwargs}`` object → the parsed
    :class:`~tai42_contract.template.TemplatedText` (a stored schema named by id, or inline
    templated content); any other JSON object → the inline schema dict itself; anything else
    → a loud 400. A by-id resource is not fetched here — it is rendered, parsed and validated
    at the save-time dry-run bake and at the point of use.
    """
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


def _read_map(field: str, value: Any) -> TemplatedText | str | None:
    """An optional author-set jq map — the ``TemplatedText | str`` union — from a request value.

    ``null`` → ``None``; a string or an already-parsed
    :class:`~tai42_contract.template.TemplatedText` → itself; a ``{content|id, kwargs}`` object → the
    parsed :class:`~tai42_contract.template.TemplatedText`; anything else → a loud 400. The program
    is rendered and compiled at the save-time document check, not here.
    """
    if value is None or isinstance(value, str | TemplatedText):
        return value
    if not isinstance(value, dict):
        raise BadRequestError(f"'{field}' must be a jq program string or a stored-body reference")
    try:
        return TemplatedText.model_validate(value)
    except ValidationError as exc:
        raise BadRequestError(f"invalid '{field}': {exc.errors(include_url=False)}") from exc


def read_input_map(value: Any) -> TemplatedText | str | None:
    """The optional author-set input map from a request value (see :func:`_read_map`)."""
    return _read_map("input_map", value)


def read_output_map(value: Any) -> TemplatedText | str | None:
    """The optional author-set output map from a request value (see :func:`_read_map`)."""
    return _read_map("output_map", value)


def read_state_binding(value: Any) -> StateBinding | None:
    """The optional door-layer state binding from a request value.

    ``null`` → ``None``; a binding object → the parsed :class:`StateBinding`; a malformed shape → a
    loud 400. The HTTP-edge extractor uses this so a body binding is not silently dropped before
    the op.
    """
    if value is None:
        return None
    try:
        return StateBinding.model_validate(value)
    except ValidationError as exc:
        raise BadRequestError(f"invalid 'state_binding': {exc.errors(include_url=False)}") from exc
