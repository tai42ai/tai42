"""Rewrite an authored JSON Schema into the portable subset every provider-native grammar accepts.

The provider-native structured-output grammars reject a few constructs the full
draft-2020-12 vocabulary allows — most importantly a nullable ``"type"`` array
(``{"type": ["integer", "null"]}``), which the vendor converters pass to
``assert_never`` and raise on. :func:`to_portable_schema` rewrites such a node into
an ``anyOf`` of single-type members, each carrying only the keywords that apply to
its type, WITHOUT changing the set of instances the schema accepts: validation
against the portable form agrees with validation against the original (the K1 test
obligation). The portable form is derived at the model-binding chokepoint and never
stored — the authored schema in the flow node, the preset and the stored template
keep their original shape.

:func:`check_native_representable` reports whether a (portable) schema is one the
native grammars can carry at all: a map object (``additionalProperties`` that is a
schema) is not, because every native grammar closes objects.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

_ANNOTATION_KEYWORDS = ("title", "description", "default", "examples", "deprecated", "readOnly", "writeOnly")
_OBJECT_KEYWORDS = (
    "properties",
    "required",
    "additionalProperties",
    "patternProperties",
    "propertyNames",
    "minProperties",
    "maxProperties",
    "dependentRequired",
)
_ARRAY_KEYWORDS = (
    "items",
    "prefixItems",
    "contains",
    "minContains",
    "maxContains",
    "minItems",
    "maxItems",
    "uniqueItems",
)
_STRING_KEYWORDS = ("minLength", "maxLength", "pattern", "format")
_NUMBER_KEYWORDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")

#: Keywords whose value is itself a single schema node to recurse into.
_SCHEMA_VALUED = ("items", "contains", "propertyNames", "additionalProperties")
#: Keywords whose value is a mapping of names to schema nodes.
_SCHEMA_MAP_VALUED = ("properties", "patternProperties", "$defs")
#: Keywords whose value is a list of schema nodes.
_SCHEMA_LIST_VALUED = ("prefixItems",)


class NonPortableSchemaError(Exception):
    """An authored schema node cannot be rewritten into the native-portable subset.

    Raised by :func:`to_portable_schema` for a node that declares no ``type`` and no
    combinator and carries nothing (``const``/``enum``) to infer a type from — the
    native grammars require a concrete type, so such a node has no portable form.
    ``path`` is the JSON path of the offending node.
    """

    def __init__(self, path: str) -> None:
        """Carry the JSON ``path`` of the node with no inferable type."""
        self.path = path
        super().__init__(f"schema node at {path} has no 'type' and nothing to infer one from; it is not representable")


def _json_type_of(value: Any) -> str:
    """The JSON-Schema type name of a concrete Python ``value`` (an ``enum``/``const`` member)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _keywords_for_type(json_type: str) -> tuple[str, ...]:
    """The constraint keywords that apply to a node of ``json_type``."""
    if json_type == "object":
        return _OBJECT_KEYWORDS
    if json_type == "array":
        return _ARRAY_KEYWORDS
    if json_type == "string":
        return _STRING_KEYWORDS
    if json_type in ("integer", "number"):
        return _NUMBER_KEYWORDS
    return ()


def _portable_keyword(keyword: str, value: Any, path: str) -> Any:
    """Rewrite one keyword's value, recursing into any schema node(s) it carries."""
    if keyword in _SCHEMA_MAP_VALUED and isinstance(value, dict):
        return {name: _portable(sub, f"{path}.{keyword}.{name}") for name, sub in value.items()}
    if keyword in _SCHEMA_LIST_VALUED and isinstance(value, list):
        return [_portable(sub, f"{path}.{keyword}[{index}]") for index, sub in enumerate(value)]
    if keyword in _SCHEMA_VALUED:
        # A boolean additionalProperties/items stays as-is; a schema node recurses.
        if isinstance(value, dict):
            return _portable(value, f"{path}.{keyword}")
        return copy.deepcopy(value)
    return copy.deepcopy(value)


def _single_type_body(node: dict[str, Any], json_type: str, path: str) -> dict[str, Any]:
    """The ``{"type": json_type, ...}`` body for ``json_type``, restricted to its own keywords.

    Carries no annotation keywords (they stay on the parent of an ``anyOf``). An
    ``enum`` is partitioned to the members of this type; a ``null`` type is the bare
    ``{"type": "null"}`` node.
    """
    if json_type == "null":
        return {"type": "null"}
    body: dict[str, Any] = {"type": json_type}
    for keyword in _keywords_for_type(json_type):
        if keyword in node:
            body[keyword] = _portable_keyword(keyword, node[keyword], path)
    if "enum" in node:
        members = [value for value in node["enum"] if _json_type_of(value) == json_type]
        if members:
            body["enum"] = members
    return body


def _carry_defs(node: dict[str, Any], out: dict[str, Any], path: str) -> None:
    """Recurse and carry a ``$defs`` block (and a ``$ref``) onto the portable ``out`` node."""
    if "$defs" in node and isinstance(node["$defs"], dict):
        out["$defs"] = {name: _portable(sub, f"{path}.$defs.{name}") for name, sub in node["$defs"].items()}
    if "$ref" in node:
        out["$ref"] = node["$ref"]


def _annotations(node: dict[str, Any]) -> dict[str, Any]:
    return {keyword: copy.deepcopy(node[keyword]) for keyword in _ANNOTATION_KEYWORDS if keyword in node}


def _portable_combinator(node: dict[str, Any], path: str) -> dict[str, Any]:
    """The portable form of a node whose structure is an ``anyOf``/``oneOf``/``allOf``."""
    out = _annotations(node)
    for present in ("anyOf", "oneOf", "allOf"):
        if present in node:
            out[present] = [_portable(sub, f"{path}.{present}[{index}]") for index, sub in enumerate(node[present])]
    _carry_defs(node, out, path)
    return out


def _portable_typeless(node: dict[str, Any], path: str) -> dict[str, Any]:
    """The portable form of a type-less node, inferring the type from ``const``/``enum``.

    Raises :class:`NonPortableSchemaError` when there is nothing to infer a type from.
    """
    if "const" in node:
        out = {**_annotations(node), **_single_type_body(node, _json_type_of(node["const"]), path)}
        out["enum"] = [node["const"]]
        _carry_defs(node, out, path)
        return out
    if "enum" in node:
        types = sorted({_json_type_of(value) for value in node["enum"]})
        if len(types) == 1:
            out = {**_annotations(node), **_single_type_body(node, types[0], path)}
        else:
            out = _annotations(node)
            out["anyOf"] = [_single_type_body(node, member_type, path) for member_type in types]
        _carry_defs(node, out, path)
        return out
    raise NonPortableSchemaError(path)


def _portable(node: Any, path: str) -> Any:
    """Rewrite one schema node into its native-portable form (recursively)."""
    if not isinstance(node, dict):
        # A boolean schema (True/False) or a non-dict leaf is already portable.
        return copy.deepcopy(node)

    if any(combinator in node for combinator in ("anyOf", "oneOf", "allOf")):
        return _portable_combinator(node, path)

    if "$ref" in node:
        out = _annotations(node)
        _carry_defs(node, out, path)
        return out

    json_type = node.get("type")

    if json_type is None:
        return _portable_typeless(node, path)

    if isinstance(json_type, list):
        out = _annotations(node)
        out["anyOf"] = [_single_type_body(node, member, f"{path}|{member}") for member in json_type]
        _carry_defs(node, out, path)
        return out

    out = {**_annotations(node), **_single_type_body(node, json_type, path)}
    _carry_defs(node, out, path)
    return out


def to_portable_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return ``schema`` rewritten into the native-portable subset, without changing what it validates.

    A nullable ``"type"`` array becomes an ``anyOf`` of single-type members (each
    keeping only the keywords that apply to its type); a type-less ``enum``/``const``
    node has its type inferred from the member(s); recursion runs through
    ``properties``, ``items``, ``prefixItems``, ``anyOf``/``oneOf``/``allOf``,
    ``additionalProperties`` schemas and ``$defs``. A node with no ``type`` and
    nothing to infer one from raises :class:`NonPortableSchemaError`.
    """
    return _portable(schema, "$")


def _child_schemas(node: dict[str, Any], path: str) -> Iterator[tuple[Any, str]]:
    """Every child schema node of ``node`` with its JSON path (for a representability walk)."""
    for keyword in _SCHEMA_MAP_VALUED:
        mapping = node.get(keyword)
        if isinstance(mapping, dict):
            for name, sub in mapping.items():
                yield sub, f"{path}.{keyword}.{name}"
    for keyword in (*_SCHEMA_LIST_VALUED, "anyOf", "oneOf", "allOf"):
        members = node.get(keyword)
        if isinstance(members, list):
            for index, sub in enumerate(members):
                yield sub, f"{path}.{keyword}[{index}]"
    for keyword in ("items", "contains", "propertyNames"):
        sub = node.get(keyword)
        if isinstance(sub, dict):
            yield sub, f"{path}.{keyword}"


def _first_nonrepresentable(node: Any, path: str) -> str | None:
    """The JSON path of the first node a native grammar cannot carry, or ``None``."""
    if not isinstance(node, dict):
        return None
    if isinstance(node.get("additionalProperties"), dict):
        # A map object (schema-valued additionalProperties): every native grammar closes objects.
        return f"{path}.additionalProperties"
    for sub, sub_path in _child_schemas(node, path):
        found = _first_nonrepresentable(sub, sub_path)
        if found is not None:
            return found
    return None


def check_native_representable(schema: dict[str, Any]) -> bool:
    """Whether a (portable) ``schema`` is one the provider-native grammars can carry.

    ``False`` when the schema contains a map object (a schema-valued
    ``additionalProperties``): the native grammars close every object, so a map
    object has no native form and the plan must fall back to the tool tier (or
    refuse). ``additionalProperties`` that is a boolean, or absent, is representable.
    """
    return _first_nonrepresentable(schema, "$") is None
