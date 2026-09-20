"""Schema → ``TypedDict`` annotation tree.

Companion to the pydantic-model converter that emits a ``TypedDict`` for every
object node, so a value validated against the result round-trips to plain nested
``dict``s while still enforcing every representable constraint at every value
site. ``$ref`` resolves through ``$defs``; a recursive ``$ref`` (which a
``TypedDict`` tree cannot express) raises loudly rather than looping.
"""

from typing import Annotated, Any, Literal, NotRequired, TypedDict, Union

from tai42_kit.utils.data.json_schema_util.constraints import _map_json_type, _value_constraint_metadata


def json_schema_to_typed_dict(schema: dict[str, Any], name: str = "Response", *, max_depth: int = 50) -> Any:
    """Convert a JSON-Schema fragment into a ``TypedDict``-rooted annotation.

    A value validated against the result (via a pydantic ``TypeAdapter``, e.g.
    langchain's tool-calling structured-output path) round-trips to plain nested
    ``dict``s rather than model instances — preserving the dict shape a raw-schema
    ``response_format`` yields while still enforcing every representable constraint
    at every value site (object properties, array items/prefixItems,
    ``anyOf``/``oneOf`` members, ``additionalProperties`` map values), so an
    out-of-range integer (after ``inject_int64_bounds``) raises at parse rather
    than at serialization — wherever the integer sits.

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


def _typed_from_type(schema: dict[str, Any], name: str, ctx: "_TypedContext", max_depth: int, depth: int) -> Any:
    """The ``type``-driven tail: list-union, object, array, scalar."""
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


def _to_typed_annotation(schema: dict[str, Any], name: str, ctx: "_TypedContext", max_depth: int, depth: int) -> Any:
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

    return _typed_from_type(schema, name, ctx, max_depth, depth)


def _resolve_typed_ref(ref: str, ctx: "_TypedContext", max_depth: int, depth: int) -> Any:
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


def _typed_union(subschemas: list[Any], name: str, tag: str, ctx: "_TypedContext", max_depth: int, depth: int) -> Any:
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


def _typed_allof(schema: dict[str, Any], name: str, ctx: "_TypedContext", max_depth: int, depth: int) -> Any:
    # allOf is an intersection; a Python signature can express it only by merging
    # the object subschemas' properties into one TypedDict. A non-object member
    # (e.g. a bare $ref or scalar constraint) has no field form, so the whole node
    # falls back to Any — the structural ceiling the pydantic converter documents.
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


def _typed_object(schema: dict[str, Any], name: str, ctx: "_TypedContext", max_depth: int, depth: int) -> Any:
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


def _typed_array(schema: dict[str, Any], name: str, ctx: "_TypedContext", max_depth: int, depth: int) -> Any:
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
