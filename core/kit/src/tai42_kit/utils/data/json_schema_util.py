"""JSON-Schema utilities: a schema → pydantic model converter and a faithful
draft-2020-12 validator.

``json_schema_to_pydantic_model`` turns a JSON-Schema fragment (objects, arrays,
refs, ``anyOf``/``allOf``/``oneOf``, enums/consts, nullable unions) into a
dynamically built pydantic model (or plain annotation). Used by tool adapters to
synthesize input/output models, but it depends on nothing tool-specific — any
caller with a JSON-Schema dict can use it.

Recursion is bounded by ``max_depth`` (default 50): a schema nested past the
bound raises ``ValueError`` rather than blowing the interpreter stack, and an
empty ``anyOf``/``oneOf`` raises a named ``ValueError`` instead of an opaque
``TypeError``.

Representable constraints. Value constraints (``minimum``/``maximum``/
``exclusiveMinimum``/``exclusiveMaximum``/``multipleOf``/``minLength``/
``maxLength``/``minItems``/``maxItems``/``pattern``) are carried onto the
generated annotation as ``Annotated`` metadata at every site that owns one —
object PROPERTIES and array ITEMS — so they survive the round-trip into the
re-derived schema and are enforced by pydantic. ``format`` rides along as
``json_schema_extra`` (an annotation that survives the round-trip, not a pydantic
assertion). A top-level bare scalar (``{"type": "string", ...}`` with no object
wrapper) maps to a plain annotation and carries no constraint metadata.

Structural ceiling. A Python signature cannot express every JSON-Schema
construct: ``oneOf`` collapses to a plain ``Union`` (its exactly-one/XOR meaning
is lost), and ``not`` / conditional (``if``/``then``/``else``) subschemas fall
back to ``Any``. The converter cannot enforce these — ``validate_against_json_schema``
does (it enforces every keyword directly).

``validate_against_json_schema`` validates a Python value against a JSON Schema
with the ``jsonschema`` library (draft 2020-12): every keyword is enforced. It
meta-schema-checks the schema first — an invalid schema raises
``InvalidJsonSchemaError`` — then raises ``JsonSchemaValidationError`` carrying
the JSON-path and offending value of a mismatch. A conforming value returns
``None``.
"""

import builtins
import keyword
import re
from typing import Annotated, Any, ForwardRef, Literal, Union

import annotated_types as at
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, create_model

__all__ = [
    "InvalidJsonSchemaError",
    "JsonSchemaValidationError",
    "check_json_schema",
    "json_schema_to_pydantic_model",
    "validate_against_json_schema",
]

reserved_words = set(keyword.kwlist) | set(dir(builtins))


def _handle_ref_schema(schema: dict[str, Any], parent_models: dict[str, type]) -> Any:
    ref_path = schema["$ref"].split("/")[-1]
    return parent_models.get(ref_path, ForwardRef(ref_path))


def _handle_anyof_schema(
    schema: dict[str, Any], model_name: str, parent_models: dict[str, type], max_depth: int, _depth: int
) -> Any:
    subschemas = schema["anyOf"]
    if not subschemas:
        raise ValueError(f"JSON schema 'anyOf' must contain at least one subschema (at {model_name!r})")
    # Dynamic runtime Union over a tuple of types; PEP-604 `|` has no
    # dynamic-tuple form, so the typing.Union subscript is required here.
    return Union[  # noqa: UP007
        tuple(
            json_schema_to_pydantic_model(
                s, f"{model_name}_any{i}", parent_models=parent_models, max_depth=max_depth, _depth=_depth + 1
            )
            for i, s in enumerate(subschemas)
        )
    ]


def _handle_allof_schema(
    schema: dict[str, Any], model_name: str, parent_models: dict[str, type], max_depth: int, _depth: int
) -> Any:
    base_models = [
        json_schema_to_pydantic_model(s, f"{model_name}_all{i}", parent_models, max_depth=max_depth, _depth=_depth + 1)
        for i, s in enumerate(schema["allOf"])
    ]

    base_models = [m for m in base_models if isinstance(m, type) and issubclass(m, BaseModel)]
    if not base_models:
        return Any

    config = ConfigDict(from_attributes=True)
    model = create_model(model_name, __base__=tuple(base_models), __module__="pydantic_generated", __config__=config)

    model.__doc__ = schema.get("description", "")
    return model


def _handle_oneof_schema(
    schema: dict[str, Any], model_name: str, parent_models: dict[str, type], max_depth: int, _depth: int
) -> Any:
    subschemas = schema["oneOf"]
    if not subschemas:
        raise ValueError(f"JSON schema 'oneOf' must contain at least one subschema (at {model_name!r})")
    # Dynamic runtime Union over a tuple of types; PEP-604 `|` has no
    # dynamic-tuple form, so the typing.Union subscript is required here.
    return Union[  # noqa: UP007
        tuple(
            json_schema_to_pydantic_model(
                s, f"{model_name}_one{i}", parent_models, max_depth=max_depth, _depth=_depth + 1
            )
            for i, s in enumerate(subschemas)
        )
    ]


def _handle_const_schema(schema: dict[str, Any]) -> Any:
    return Literal[schema["const"]]


def _handle_enum_schema(schema: dict[str, Any]) -> Any:
    return Literal[tuple(schema["enum"])]


def _handle_nullable_type_schema(
    schema: dict[str, Any],
    model_name: str,
    parent_models: dict[str, type],
    is_top_level: bool,
    max_depth: int,
    _depth: int,
) -> Any:
    json_types = schema["type"]

    non_null_types = [t for t in json_types if t != "null"]

    if len(non_null_types) == 1:
        single_type = non_null_types[0]

        if single_type == "object":
            return _handle_object_schema(schema, model_name, parent_models, is_top_level, max_depth, _depth) | None

        if single_type == "array":
            return _handle_array_schema(schema, model_name, parent_models, max_depth, _depth) | None

        return _map_json_type(single_type) | None

    # Dynamic runtime Union over a tuple of types; PEP-604 `|` has no
    # dynamic-tuple form, so the typing.Union subscript is required here.
    inner_union = Union[tuple(_map_json_type(t) for t in non_null_types)]  # noqa: UP007
    return inner_union | None


def _matches_type(t: Any, name: str) -> bool:
    # A JSON-Schema ``type`` may be a single string or a list (a nullable prop is
    # commonly ``["string", "null"]``); a value keyword gates on the base type
    # whether it is stated directly or as one member of the list.
    return t == name or (isinstance(t, list) and name in t)


def _value_constraint_metadata(prop_schema: dict[str, Any]) -> list[Any]:
    """Metadata objects carrying the schema's value constraints, gated by the
    declared ``type`` (a keyword on a non-matching type is a spec-level no-op)."""
    t = prop_schema.get("type")
    metadata: list[Any] = []

    if _matches_type(t, "number") or _matches_type(t, "integer"):
        if "minimum" in prop_schema:
            metadata.append(at.Ge(prop_schema["minimum"]))
        if "maximum" in prop_schema:
            metadata.append(at.Le(prop_schema["maximum"]))
        if "exclusiveMinimum" in prop_schema:
            metadata.append(at.Gt(prop_schema["exclusiveMinimum"]))
        if "exclusiveMaximum" in prop_schema:
            metadata.append(at.Lt(prop_schema["exclusiveMaximum"]))
        if "multipleOf" in prop_schema:
            metadata.append(at.MultipleOf(prop_schema["multipleOf"]))

    if _matches_type(t, "string"):
        if "minLength" in prop_schema:
            metadata.append(at.MinLen(prop_schema["minLength"]))
        if "maxLength" in prop_schema:
            metadata.append(at.MaxLen(prop_schema["maxLength"]))
        if "pattern" in prop_schema:
            metadata.append(StringConstraints(pattern=prop_schema["pattern"]))

    if _matches_type(t, "array"):
        if "minItems" in prop_schema:
            metadata.append(at.MinLen(prop_schema["minItems"]))
        if "maxItems" in prop_schema:
            metadata.append(at.MaxLen(prop_schema["maxItems"]))

    return metadata


def _handle_object_schema(
    schema: dict[str, Any],
    model_name: str,
    parent_models: dict[str, type],
    is_top_level: bool,
    max_depth: int,
    _depth: int,
) -> Any:
    additional_props = schema.get("additionalProperties", False)
    has_additional = "additionalProperties" in schema
    properties = schema.get("properties", {})

    if additional_props and not properties:
        value_type = (
            Any
            if additional_props is True
            else json_schema_to_pydantic_model(
                additional_props,
                f"{model_name}_Value",
                parent_models=parent_models,
                max_depth=max_depth,
                _depth=_depth + 1,
            )
            if isinstance(additional_props, dict)
            else Any
        )
        return dict[str, value_type]

    fields = {}
    sanitized_origins: dict[str, str] = {}
    required = set(schema.get("required", []))
    for prop_name, prop_schema in properties.items():
        prop_model: Any = json_schema_to_pydantic_model(
            prop_schema, f"{model_name}_{prop_name}", parent_models, max_depth=max_depth, _depth=_depth + 1
        )

        # Carry the schema's value constraints as Annotated metadata on the
        # property annotation BEFORE any ``| None`` wrap, so they survive the
        # optional-property union into the re-derived schema and the coercion
        # validator.
        metadata = _value_constraint_metadata(prop_schema)
        if metadata:
            prop_model = Annotated[prop_model, *metadata]

        default = ... if prop_name in required else prop_schema.get("default", None)
        annotation = prop_model if prop_name in required else prop_model | None

        # Sanitize property name
        sanitized_name = prop_name
        if sanitized_name.startswith("$"):
            sanitized_name = sanitized_name[1:]
        sanitized_name = re.sub(r"^-", "neg_", sanitized_name)
        sanitized_name = re.sub(r"[@]", "at_", sanitized_name)
        sanitized_name = re.sub(r"[^a-zA-Z0-9_]", "_", sanitized_name)
        # Pydantic forbids field names that start with a digit or underscore, so
        # prefix those with a letter; the alias below keeps the original JSON key.
        if sanitized_name and (sanitized_name[0].isdigit() or sanitized_name[0] == "_"):
            sanitized_name = "field_" + sanitized_name
        while sanitized_name in reserved_words or keyword.iskeyword(sanitized_name):
            sanitized_name += "_"

        alias = prop_name if sanitized_name != prop_name else None

        # Distinct properties may sanitize to the same python field name (e.g.
        # "a-b" and "a_b" both become "a_b"), which would overwrite one field and
        # silently drop the other; refuse the schema instead.
        if sanitized_name in sanitized_origins:
            raise ValueError(
                f"properties {sanitized_origins[sanitized_name]!r} and {prop_name!r} "
                f"both sanitize to field name {sanitized_name!r}"
            )
        sanitized_origins[sanitized_name] = prop_name

        # ``format`` has no validation semantics — carry it as json_schema_extra
        # so it survives into the re-derived schema.
        fmt = prop_schema.get("format")
        json_schema_extra = {"format": fmt} if fmt is not None else None

        fields[sanitized_name] = (
            annotation,
            Field(
                default=default,
                title=prop_schema.get("title"),
                description=prop_schema.get("description"),
                alias=alias,
                json_schema_extra=json_schema_extra,
            ),
        )

    # 'extra' must be passed into create_model: pydantic v2 builds the field
    # collection at class-creation time and never re-reads model_config, so a
    # post-hoc model_config['extra'] = 'allow' is silently ignored.
    #   additionalProperties truthy  -> 'allow'  (retain extra keys)
    #   additionalProperties == false -> 'forbid' (reject extra keys)
    #   additionalProperties absent   -> 'ignore' (pydantic default). Deliberate
    #     deviation from strict JSON-Schema (absent == allow): for tool-arg
    #     coercion, dropping unknown keys is the pragmatic safe default.
    if additional_props:
        extra_policy = "allow"
    elif has_additional:
        extra_policy = "forbid"
    else:
        extra_policy = "ignore"
    config = ConfigDict(from_attributes=True, extra=extra_policy)
    model = create_model(model_name, __base__=BaseModel, __module__="pydantic_generated", __config__=config, **fields)

    if is_top_level:
        types_namespace = dict(parent_models)
        types_namespace[model_name] = model
        for v in parent_models.values():
            if isinstance(v, type) and issubclass(v, BaseModel):
                v.model_rebuild(_types_namespace=types_namespace)
        model.model_rebuild(_types_namespace=types_namespace)

    model.__doc__ = schema.get("description", "")
    return model


def _handle_array_schema(
    schema: dict[str, Any], model_name: str, parent_models: dict[str, type], max_depth: int, _depth: int
) -> Any:
    item_schema = schema.get("items", {})
    if not item_schema:
        return list[Any]

    item_model = json_schema_to_pydantic_model(
        item_schema, f"{model_name}_Item", parent_models, max_depth=max_depth, _depth=_depth + 1
    )

    # Array items own a value site too: carry the item schema's constraints as
    # Annotated metadata on the element type (``list[Annotated[T, ...]]``) so an
    # item constraint (e.g. an item ``pattern`` or ``minLength``) is enforced and
    # re-emitted, mirroring the per-property carry in ``_handle_object_schema``.
    metadata = _value_constraint_metadata(item_schema)
    if metadata:
        item_model = Annotated[item_model, *metadata]
    return list[item_model]


def _map_json_type(json_type: str) -> Any:
    type_mapping = {"string": str, "number": float, "integer": int, "boolean": bool, "null": type(None)}
    return type_mapping.get(json_type, Any)


def json_schema_to_pydantic_model(
    schema: dict[str, Any],
    model_name: str = "RootModel",
    parent_models: dict[str, type] | None = None,
    *,
    max_depth: int = 50,
    _depth: int = 0,
) -> Any:
    if _depth > max_depth:
        raise ValueError(f"JSON schema nesting exceeds max_depth={max_depth} (at {model_name!r})")

    is_top_level = parent_models is None
    parent_models = parent_models or {}

    if "$defs" in schema:
        for def_name, def_schema in schema["$defs"].items():
            if def_name not in parent_models:
                parent_models[def_name] = json_schema_to_pydantic_model(
                    def_schema, def_name, parent_models, max_depth=max_depth, _depth=_depth + 1
                )

    if "$ref" in schema:
        return _handle_ref_schema(schema, parent_models)

    if "anyOf" in schema:
        return _handle_anyof_schema(schema, model_name, parent_models, max_depth, _depth)

    if "allOf" in schema:
        return _handle_allof_schema(schema, model_name, parent_models, max_depth, _depth)

    if "oneOf" in schema:
        return _handle_oneof_schema(schema, model_name, parent_models, max_depth, _depth)

    if "const" in schema:
        return _handle_const_schema(schema)

    if "enum" in schema:
        return _handle_enum_schema(schema)

    if "not" in schema:
        # Fallback to Any, as Pydantic doesn't support direct "not" constraints
        return Any

    if "type" not in schema:
        return Any

    json_type = schema["type"]

    if isinstance(json_type, list):
        if "null" in json_type:
            return _handle_nullable_type_schema(schema, model_name, parent_models, is_top_level, max_depth, _depth)
        else:
            # Dynamic runtime Union over a tuple of types; PEP-604 `|` has no
            # dynamic-tuple form, so the typing.Union subscript is required here.
            return Union[tuple(_map_json_type(t) for t in json_type if t != "null")]  # noqa: UP007

    if json_type == "object":
        return _handle_object_schema(schema, model_name, parent_models, is_top_level, max_depth, _depth)

    if json_type == "array":
        return _handle_array_schema(schema, model_name, parent_models, max_depth, _depth)

    return _map_json_type(json_type)


class JsonSchemaError(Exception):
    """Base for ``validate_against_json_schema`` failures."""


class InvalidJsonSchemaError(JsonSchemaError):
    """The supplied schema is not a valid draft-2020-12 JSON Schema.

    Raised by the meta-schema check before any instance is examined, so a
    malformed schema fails loudly instead of silently mis-validating.
    """


class JsonSchemaValidationError(JsonSchemaError):
    """The instance does not conform to the (valid) schema.

    ``json_path`` is the ``jsonschema`` JSONPath of the reported offending node
    (e.g. ``$.items[0].name``) and ``offending_value`` is the value found there.
    """

    def __init__(self, message: str, *, json_path: str, offending_value: Any) -> None:
        self.json_path = json_path
        self.offending_value = offending_value
        super().__init__(message)


def check_json_schema(schema: dict[str, Any]) -> None:
    """Verify ``schema`` is a valid draft-2020-12 JSON Schema.

    Returns ``None`` when the schema is well-formed; raises
    ``InvalidJsonSchemaError`` loudly when it is not. Use this to reject a
    malformed schema at configuration time, before any instance exists to validate.
    """
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InvalidJsonSchemaError(f"invalid JSON schema: {exc.message}") from exc


def validate_against_json_schema(instance: Any, schema: dict[str, Any]) -> None:
    """Validate ``instance`` against ``schema`` (JSON Schema draft 2020-12).

    Returns ``None`` when the instance conforms. Otherwise raises loudly — never
    silently passing or degrading:

    * ``InvalidJsonSchemaError`` if ``schema`` itself fails the draft-2020-12
      meta-schema (checked first, so a broken schema can never mis-validate an
      instance into a false pass), and
    * ``JsonSchemaValidationError`` for a mismatch, carrying the JSON-path and
      offending value of the reported failure.

    Every JSON-Schema keyword is enforced (this is the faithful path that the
    signature-bound converter cannot fully express — see the module docstring).
    """
    check_json_schema(schema)

    validator = Draft202012Validator(schema)
    error = best_match(validator.iter_errors(instance))
    if error is not None:
        raise JsonSchemaValidationError(
            f"value does not match schema at {error.json_path}: {error.message}",
            json_path=error.json_path,
            offending_value=error.instance,
        ) from error
