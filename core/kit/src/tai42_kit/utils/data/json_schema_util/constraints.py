"""Schema-keyword → type / metadata mapping shared by both schema converters.

Maps a JSON-Schema ``type`` to its Python type and turns a schema's value
constraints (numeric bounds, string length/pattern, array length) into the
``annotated_types`` metadata a generated annotation carries.
"""

from typing import Any

import annotated_types as at
from pydantic import StringConstraints


def _map_json_type(json_type: str) -> Any:
    type_mapping = {"string": str, "number": float, "integer": int, "boolean": bool, "null": type(None)}
    return type_mapping.get(json_type, Any)


def _matches_type(t: Any, name: str) -> bool:
    # A JSON-Schema ``type`` may be a single string or a list (a nullable prop is
    # commonly ``["string", "null"]``); a value keyword gates on the base type
    # whether it is stated directly or as one member of the list.
    return t == name or (isinstance(t, list) and name in t)


def _numeric_constraints(schema: dict[str, Any]) -> list[Any]:
    metadata: list[Any] = []
    if "minimum" in schema:
        metadata.append(at.Ge(schema["minimum"]))
    if "maximum" in schema:
        metadata.append(at.Le(schema["maximum"]))
    if "exclusiveMinimum" in schema:
        metadata.append(at.Gt(schema["exclusiveMinimum"]))
    if "exclusiveMaximum" in schema:
        metadata.append(at.Lt(schema["exclusiveMaximum"]))
    if "multipleOf" in schema:
        metadata.append(at.MultipleOf(schema["multipleOf"]))
    return metadata


def _string_constraints(schema: dict[str, Any]) -> list[Any]:
    metadata: list[Any] = []
    if "minLength" in schema:
        metadata.append(at.MinLen(schema["minLength"]))
    if "maxLength" in schema:
        metadata.append(at.MaxLen(schema["maxLength"]))
    if "pattern" in schema:
        metadata.append(StringConstraints(pattern=schema["pattern"]))
    return metadata


def _array_constraints(schema: dict[str, Any]) -> list[Any]:
    metadata: list[Any] = []
    if "minItems" in schema:
        metadata.append(at.MinLen(schema["minItems"]))
    if "maxItems" in schema:
        metadata.append(at.MaxLen(schema["maxItems"]))
    return metadata


def _value_constraint_metadata(prop_schema: dict[str, Any]) -> list[Any]:
    """Metadata objects carrying the schema's value constraints, gated by the
    declared ``type`` (a keyword on a non-matching type is a spec-level no-op)."""
    t = prop_schema.get("type")
    metadata: list[Any] = []
    if _matches_type(t, "number") or _matches_type(t, "integer"):
        metadata += _numeric_constraints(prop_schema)
    if _matches_type(t, "string"):
        metadata += _string_constraints(prop_schema)
    if _matches_type(t, "array"):
        metadata += _array_constraints(prop_schema)
    return metadata
