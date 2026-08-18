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
import copy
import keyword
import re
from typing import Annotated, Any, ForwardRef, Literal, NotRequired, TypedDict, Union

import annotated_types as at
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, create_model

__all__ = [
    "INT64_MAX",
    "INT64_MIN",
    "MSGPACK_INT_MAX",
    "MSGPACK_INT_MIN",
    "InvalidJsonSchemaError",
    "JsonSchemaValidationError",
    "check_json_schema",
    "find_oversized_int",
    "inject_int64_bounds",
    "json_schema_to_pydantic_model",
    "json_schema_to_typed_dict",
    "validate_against_json_schema",
]

# Platform serialization ceiling: the checkpoint serializer encodes an ``int``
# as a native msgpack integer, which is bounded to signed 64-bit. An integer
# outside this range has no native encoding and aborts serialization, so every
# integer schema node is tightened to it before a value can be produced.
INT64_MIN = -9223372036854775808
INT64_MAX = 9223372036854775807

# Native msgpack integer encoding range: a signed 64-bit floor through an
# unsigned 64-bit ceiling. msgpack encodes any integer in this range; an integer
# outside it aborts the serializer. Wider than the int64 platform range above:
# an unsigned value in ``(INT64_MAX, MSGPACK_INT_MAX]`` encodes fine at the
# serializer even though it overflows a signed-int64 field.
MSGPACK_INT_MIN = -9223372036854775808
MSGPACK_INT_MAX = 18446744073709551615


def find_oversized_int(
    obj: Any,
    *,
    minimum: int,
    maximum: int,
    path: str = "",
    depth: int = 0,
    seen: set[int] | None = None,
) -> tuple[str, int] | None:
    """First ``(path, value)`` in ``obj`` whose integer falls outside
    ``[minimum, maximum]``, or ``None``.

    Walks mappings, sequences, and object/model attributes, guarding against
    cycles and unbounded depth. The bound is caller-supplied: the checkpoint
    guard scans the native msgpack range (``MSGPACK_INT_MIN``/``MSGPACK_INT_MAX``)
    to name the true encode-failure culprit, while structured-output validation
    scans the stricter int64 platform range (``INT64_MIN``/``INT64_MAX``)."""
    if depth > 200:
        return None
    # bool is an int subclass but always encodes; only true integers can overflow.
    if isinstance(obj, bool):
        return None
    if isinstance(obj, int):
        return (path or "<root>", obj) if obj < minimum or obj > maximum else None

    if isinstance(obj, (str, bytes, bytearray)):
        return None

    seen = seen if seen is not None else set()
    if id(obj) in seen:
        return None
    seen.add(id(obj))

    if isinstance(obj, dict):
        children = [(f"{path}[{key!r}]", value) for key, value in obj.items()]
    elif isinstance(obj, (list, tuple, set, frozenset)):
        children = [(f"{path}[{index}]", value) for index, value in enumerate(obj)]
    elif hasattr(obj, "__dict__"):
        children = [(f"{path}.{name}", value) for name, value in vars(obj).items()]
    else:
        return None

    for child_path, value in children:
        found = find_oversized_int(value, minimum=minimum, maximum=maximum, path=child_path, depth=depth + 1, seen=seen)
        if found is not None:
            return found
    return None


reserved_words = set(keyword.kwlist) | set(dir(builtins))


def inject_int64_bounds(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``schema`` with every integer node tightened to the
    platform int64 range.

    For each integer-typed node the bounds are tightened, never loosened:
    ``minimum = max(existing, INT64_MIN)`` and ``maximum = min(existing, INT64_MAX)``
    (an absent bound is set to the int64 edge). Walks every nested site an integer
    can hide in — ``properties``, ``patternProperties``, ``items``, ``prefixItems``,
    ``additionalProperties``, ``anyOf``/``oneOf``/``allOf`` and ``$defs`` (a ``$ref``
    is bounded through its ``$defs`` target). ``number`` (float) nodes are left
    untouched — only ``integer`` overflows the msgpack integer encoding.
    """
    result = copy.deepcopy(schema)
    _tighten_int64(result)
    return result


def _tighten_int64(node: Any) -> None:
    if not isinstance(node, dict):
        return

    node_type = node.get("type")
    if node_type == "integer" or (isinstance(node_type, list) and "integer" in node_type):
        node["minimum"] = max(node["minimum"], INT64_MIN) if "minimum" in node else INT64_MIN
        node["maximum"] = min(node["maximum"], INT64_MAX) if "maximum" in node else INT64_MAX

    for mapping_key in ("properties", "patternProperties", "$defs"):
        mapping = node.get(mapping_key)
        if isinstance(mapping, dict):
            for sub in mapping.values():
                _tighten_int64(sub)

    for list_key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        subschemas = node.get(list_key)
        if isinstance(subschemas, list):
            for sub in subschemas:
                _tighten_int64(sub)

    items = node.get("items")
    if isinstance(items, dict):
        _tighten_int64(items)
    elif isinstance(items, list):
        for sub in items:
            _tighten_int64(sub)

    additional = node.get("additionalProperties")
    if isinstance(additional, dict):
        _tighten_int64(additional)


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


def json_schema_to_typed_dict(schema: dict[str, Any], name: str = "Response", *, max_depth: int = 50) -> Any:
    """Convert a JSON-Schema fragment into a ``TypedDict``-rooted annotation.

    Companion to :func:`json_schema_to_pydantic_model` that emits a ``TypedDict``
    for every object node. A value validated against the result (via a pydantic
    ``TypeAdapter``, e.g. langchain's tool-calling structured-output path)
    round-trips to plain nested ``dict``s rather than model instances — preserving
    the dict shape a raw-schema ``response_format`` yields while still enforcing
    every representable constraint at every value site (object properties, array
    items/prefixItems, ``anyOf``/``oneOf`` members, ``additionalProperties`` map
    values), so an out-of-range integer (after :func:`inject_int64_bounds`) raises
    at parse rather than at serialization — wherever the integer sits.

    An object node is named by its ``title`` when present, so a union/``oneOf`` of
    titled variants fans out into tool specs whose names match the variant titles.
    ``$ref`` resolves through ``$defs``; a recursive ``$ref`` (which a ``TypedDict``
    tree cannot express) raises loudly rather than looping.
    """
    ctx: _TypedContext = {"schemas": schema.get("$defs", {}), "built": {}, "building": set()}
    return _to_typed_annotation(schema, name, ctx, max_depth, 0)


class _TypedContext(TypedDict):
    schemas: dict[str, Any]
    built: dict[str, Any]
    building: set[str]


def _to_typed_annotation(schema: dict[str, Any], name: str, ctx: _TypedContext, max_depth: int, depth: int) -> Any:
    if depth > max_depth:
        raise ValueError(f"JSON schema nesting exceeds max_depth={max_depth} (at {name!r})")

    if "$ref" in schema:
        return _resolve_typed_ref(schema["$ref"], ctx, max_depth, depth)
    if "anyOf" in schema:
        return _typed_union(schema["anyOf"], name, "any", ctx, max_depth, depth)
    if "oneOf" in schema:
        return _typed_union(schema["oneOf"], name, "one", ctx, max_depth, depth)
    if "allOf" in schema:
        return _typed_allof(schema, name, ctx, max_depth, depth)
    if "const" in schema:
        return Literal[schema["const"]]
    if "enum" in schema:
        return Literal[tuple(schema["enum"])]
    if "not" in schema:
        return Any
    if "type" not in schema:
        return Any

    json_type = schema["type"]
    if isinstance(json_type, list):
        parts: list[Any] = []
        for member in json_type:
            if member == "null":
                parts.append(type(None))
            elif member == "object":
                parts.append(_typed_object(schema, name, ctx, max_depth, depth))
            elif member == "array":
                parts.append(_typed_array(schema, name, ctx, max_depth, depth))
            else:
                parts.append(_map_json_type(member))
        # Dynamic runtime Union over a tuple of types; PEP-604 `|` has no
        # dynamic-tuple form, so the typing.Union subscript is required here.
        return Union[tuple(parts)]  # noqa: UP007

    if json_type == "object":
        return _typed_object(schema, name, ctx, max_depth, depth)
    if json_type == "array":
        return _typed_array(schema, name, ctx, max_depth, depth)
    return _map_json_type(json_type)


def _resolve_typed_ref(ref: str, ctx: _TypedContext, max_depth: int, depth: int) -> Any:
    ref_name = ref.split("/")[-1]
    if ref_name in ctx["built"]:
        return ctx["built"][ref_name]
    if ref_name not in ctx["schemas"]:
        raise ValueError(f"JSON schema $ref {ref!r} has no matching $defs entry")
    if ref_name in ctx["building"]:
        raise ValueError(f"JSON schema $ref {ref!r} is recursive; a TypedDict tree cannot express a cycle")
    ctx["building"].add(ref_name)
    built = _to_typed_annotation(ctx["schemas"][ref_name], ref_name, ctx, max_depth, depth + 1)
    ctx["building"].discard(ref_name)
    ctx["built"][ref_name] = built
    return built


def _typed_union(subschemas: list[Any], name: str, tag: str, ctx: _TypedContext, max_depth: int, depth: int) -> Any:
    if not subschemas:
        raise ValueError(f"JSON schema union must contain at least one subschema (at {name!r})")
    members: list[Any] = []
    for i, sub in enumerate(subschemas):
        annotation = _to_typed_annotation(sub, f"{name}_{tag}{i}", ctx, max_depth, depth + 1)
        # A union member owns a value site too: carry its scalar constraints (e.g.
        # an injected int64 bound on an integer member) as Annotated metadata so
        # the member's constraint is enforced, mirroring the property/item carry.
        # An object member has no scalar constraint, so this adds nothing there.
        metadata = _value_constraint_metadata(sub) if isinstance(sub, dict) else []
        if metadata:
            annotation = Annotated[annotation, *metadata]
        members.append(annotation)
    # Dynamic runtime Union over a tuple of types; PEP-604 `|` has no dynamic-tuple
    # form, so the typing.Union subscript is required here.
    return Union[tuple(members)]  # noqa: UP007


def _typed_allof(schema: dict[str, Any], name: str, ctx: _TypedContext, max_depth: int, depth: int) -> Any:
    # allOf is an intersection; a Python signature can express it only by merging
    # the object subschemas' properties into one TypedDict. A non-object member
    # (e.g. a bare $ref or scalar constraint) has no field form, so the whole node
    # falls back to Any — the structural ceiling json_schema_to_pydantic_model documents.
    merged_properties: dict[str, Any] = {}
    merged_required: list[str] = []
    for sub in schema["allOf"]:
        if not isinstance(sub, dict) or (sub.get("type") != "object" and "properties" not in sub):
            return Any
        merged_properties.update(sub.get("properties", {}))
        merged_required.extend(sub.get("required", []))
    combined = {
        "type": "object",
        "title": schema.get("title") or name,
        "properties": merged_properties,
        "required": merged_required,
    }
    return _typed_object(combined, name, ctx, max_depth, depth)


def _typed_object(schema: dict[str, Any], name: str, ctx: _TypedContext, max_depth: int, depth: int) -> Any:
    title = schema.get("title") or name
    properties = schema.get("properties", {})
    additional = schema.get("additionalProperties", False)

    if additional and not properties:
        if isinstance(additional, dict):
            value_type = _to_typed_annotation(additional, f"{title}_Value", ctx, max_depth, depth + 1)
            # The map value owns a value site too: carry its scalar constraints
            # (e.g. an injected int64 bound) as Annotated metadata so the value's
            # constraint is enforced, mirroring the property/item carry.
            metadata = _value_constraint_metadata(additional)
            if metadata:
                value_type = Annotated[value_type, *metadata]
        else:
            value_type = Any
        return dict[str, value_type]

    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        annotation = _to_typed_annotation(prop_schema, f"{title}_{prop_name}", ctx, max_depth, depth + 1)
        metadata = _value_constraint_metadata(prop_schema)
        if metadata:
            annotation = Annotated[annotation, *metadata]
        if prop_name not in required:
            annotation = NotRequired[annotation]
        fields[prop_name] = annotation

    # Dynamic functional TypedDict: the name and field map are built at runtime,
    # which the static form cannot express.
    return TypedDict(title, fields)  # pyright: ignore[reportArgumentType]


def _typed_array(schema: dict[str, Any], name: str, ctx: _TypedContext, max_depth: int, depth: int) -> Any:
    item_schema = schema.get("items")
    if isinstance(item_schema, dict) and item_schema:
        item_annotation = _to_typed_annotation(item_schema, f"{name}_Item", ctx, max_depth, depth + 1)
        metadata = _value_constraint_metadata(item_schema)
        if metadata:
            item_annotation = Annotated[item_annotation, *metadata]
        return list[item_annotation]

    prefix = schema.get("prefixItems")
    if isinstance(prefix, list) and prefix:
        variants: list[Any] = []
        for i, sub in enumerate(prefix):
            annotation = _to_typed_annotation(sub, f"{name}_Item{i}", ctx, max_depth, depth + 1)
            metadata = _value_constraint_metadata(sub)
            if metadata:
                annotation = Annotated[annotation, *metadata]
            variants.append(annotation)
        # Dynamic runtime Union over a tuple of types; PEP-604 `|` has no
        # dynamic-tuple form, so the typing.Union subscript is required here.
        return list[Union[tuple(variants)]]  # noqa: UP007

    return list[Any]


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
