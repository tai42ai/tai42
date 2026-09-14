"""Schema → pydantic model tree.

Turns a JSON-Schema fragment (objects, arrays, refs, ``anyOf``/``allOf``/
``oneOf``, enums/consts, nullable unions) into a dynamically built pydantic model
or plain annotation. Value constraints ride along as ``Annotated`` metadata;
recursion is bounded by ``max_depth``.

Structural ceiling. A Python signature cannot express every JSON-Schema
construct: ``oneOf`` collapses to a plain ``Union`` (its exactly-one/XOR meaning
is lost), and ``not`` / conditional (``if``/``then``/``else``) subschemas fall
back to ``Any``. The faithful validator enforces those keywords directly.
"""

import builtins
import keyword
import re
from typing import Annotated, Any, ForwardRef, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, create_model

from tai42_kit.utils.data.json_schema_util.constraints import _map_json_type, _value_constraint_metadata

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


def _sanitize_field_name(prop_name: str) -> str:
    """The pydantic-legal field name for a JSON property key: strip a leading
    ``$``, rewrite ``-``/``@``/non-alnum runs, letter-prefix a digit/underscore
    start, and suffix a reserved word so it never shadows a builtin or keyword."""
    sanitized_name = prop_name
    if sanitized_name.startswith("$"):
        sanitized_name = sanitized_name[1:]
    sanitized_name = re.sub(r"^-", "neg_", sanitized_name)
    sanitized_name = re.sub(r"[@]", "at_", sanitized_name)
    sanitized_name = re.sub(r"[^a-zA-Z0-9_]", "_", sanitized_name)
    # Pydantic forbids field names that start with a digit or underscore, so
    # prefix those with a letter; the alias keeps the original JSON key.
    if sanitized_name and (sanitized_name[0].isdigit() or sanitized_name[0] == "_"):
        sanitized_name = "field_" + sanitized_name
    while sanitized_name in reserved_words or keyword.iskeyword(sanitized_name):
        sanitized_name += "_"
    return sanitized_name


def _object_extra_policy(additional_props: Any, has_additional: bool) -> Literal["allow", "forbid", "ignore"]:
    """The pydantic ``extra`` policy an object schema's ``additionalProperties`` implies.

    'extra' must be passed into create_model: pydantic v2 builds the field
    collection at class-creation time and never re-reads model_config, so a
    post-hoc model_config['extra'] = 'allow' is silently ignored.
      additionalProperties truthy   -> 'allow'  (retain extra keys)
      additionalProperties == false -> 'forbid' (reject extra keys)
      additionalProperties absent    -> 'ignore' (pydantic default). Deliberate
        deviation from strict JSON-Schema (absent == allow): for tool-arg
        coercion, dropping unknown keys is the pragmatic safe default.
    """
    if additional_props:
        return "allow"
    if has_additional:
        return "forbid"
    return "ignore"


def _build_property_field(
    prop_name: str,
    prop_schema: dict[str, Any],
    required: set[str],
    model_name: str,
    parent_models: dict[str, type],
    max_depth: int,
    depth: int,
) -> tuple[Any, Any]:
    """One property's ``(annotation, Field)`` — the recursed model with its value
    constraints carried, the optional ``| None`` wrap, the default, and the alias
    that keeps the original JSON key when the field name was sanitized."""
    prop_model: Any = json_schema_to_pydantic_model(
        prop_schema, f"{model_name}_{prop_name}", parent_models, max_depth=max_depth, _depth=depth + 1
    )

    # Carry the schema's value constraints as Annotated metadata on the
    # property annotation BEFORE any ``| None`` wrap, so they survive the
    # optional-property union into the re-derived schema and the coercion
    # validator.
    metadata = _value_constraint_metadata(prop_schema)
    if metadata:
        prop_model = Annotated[prop_model, *metadata]

    is_required = prop_name in required
    default = ... if is_required else prop_schema.get("default")
    annotation = prop_model if is_required else prop_model | None

    sanitized_name = _sanitize_field_name(prop_name)
    alias = prop_name if sanitized_name != prop_name else None

    # ``format`` has no validation semantics — carry it as json_schema_extra
    # so it survives into the re-derived schema.
    fmt = prop_schema.get("format")
    json_schema_extra = {"format": fmt} if fmt is not None else None

    return (
        annotation,
        Field(
            default=default,
            title=prop_schema.get("title"),
            description=prop_schema.get("description"),
            alias=alias,
            json_schema_extra=json_schema_extra,
        ),
    )


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
        sanitized_name = _sanitize_field_name(prop_name)
        # Distinct properties may sanitize to the same python field name (e.g.
        # "a-b" and "a_b" both become "a_b"), which would overwrite one field and
        # silently drop the other; refuse the schema instead.
        if sanitized_name in sanitized_origins:
            raise ValueError(
                f"properties {sanitized_origins[sanitized_name]!r} and {prop_name!r} "
                f"both sanitize to field name {sanitized_name!r}"
            )
        sanitized_origins[sanitized_name] = prop_name
        fields[sanitized_name] = _build_property_field(
            prop_name, prop_schema, required, model_name, parent_models, max_depth, _depth
        )

    config = ConfigDict(from_attributes=True, extra=_object_extra_policy(additional_props, has_additional))
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


def _register_defs(schema: dict[str, Any], parent_models: dict[str, type], max_depth: int, depth: int) -> None:
    """Build and register each ``$defs`` entry into ``parent_models`` (once each);
    a schema with no ``$defs`` registers nothing."""
    for def_name, def_schema in schema.get("$defs", {}).items():
        if def_name not in parent_models:
            parent_models[def_name] = json_schema_to_pydantic_model(
                def_schema, def_name, parent_models, max_depth=max_depth, _depth=depth + 1
            )


def _pydantic_from_type(
    schema: dict[str, Any],
    model_name: str,
    parent_models: dict[str, type],
    is_top_level: bool,
    max_depth: int,
    depth: int,
) -> Any:
    """The ``type``-driven tail: nullable/list-union, object, array, scalar."""
    json_type = schema["type"]

    if isinstance(json_type, list):
        if "null" in json_type:
            return _handle_nullable_type_schema(schema, model_name, parent_models, is_top_level, max_depth, depth)
        # Dynamic runtime Union over a tuple of types; PEP-604 `|` has no
        # dynamic-tuple form, so the typing.Union subscript is required here.
        return Union[tuple(_map_json_type(t) for t in json_type if t != "null")]  # noqa: UP007

    if json_type == "object":
        return _handle_object_schema(schema, model_name, parent_models, is_top_level, max_depth, depth)

    if json_type == "array":
        return _handle_array_schema(schema, model_name, parent_models, max_depth, depth)

    return _map_json_type(json_type)


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

    _register_defs(schema, parent_models, max_depth, _depth)

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

    return _pydantic_from_type(schema, model_name, parent_models, is_top_level, max_depth, _depth)
