"""Adapt an authored JSON Schema for a provider-native grammar, value-preservingly.

The provider-native structured-output grammars reject a few constructs the full
draft-2020-12 vocabulary allows — most importantly a nullable ``"type"`` array
(``{"type": ["integer", "null"]}``), which the vendor converters pass to
``assert_never`` and raise on. Two public rewrites share one recursion:

* :func:`adapt_native_schema` applies ONLY the minimal, value-preserving adaptations
  a native grammar binder needs — a nullable/multi ``"type"`` array becomes an
  ``anyOf`` of single-type members, and a type-less ``enum``/``const`` whose members
  are all one JSON type gets that ``type``. A type-less ``enum`` whose members span
  more than one JSON type is LEFT UNCHANGED: no value-preserving minimal adaptation
  gives it a single type, so it is left bare and reported non-representable rather
  than inflated into an ``anyOf`` union.
* :func:`to_portable_schema` additionally expands such a mixed type-less ``enum`` into
  an ``anyOf`` of single-type members (one per member type). Both rewrites keep the
  set of instances the schema accepts unchanged: validation against the rewritten form
  agrees with validation against the original.

The rewrite is derived at the model-binding chokepoint and never stored — the authored
schema in the flow node, the preset and the stored template keep their original shape.

:func:`check_native_representable` reports whether a schema (as it will be sent) is one
the native grammars can carry at all, and :func:`native_representability_reason` names
the first construct that is not: a map object (``additionalProperties`` that is a
schema), because every native grammar closes objects; or a bare type-less node (no
``type``, no combinator and no ``$ref``, such as a mixed type-less ``enum`` left bare
by :func:`adapt_native_schema`), because a native grammar needs a concrete carrier.

For a caller judging a schema against a specific grammar's own documented contract,
:func:`iter_schema_nodes` walks every node with its JSON path, :func:`find_ref_cycle`
reports a recursive ``$ref``, the ``has_*`` predicates test one node for a single
construct (an external ``$ref``, ``allOf`` with ``$ref``, a numeric constraint, a string
length/pattern, an array constraint, a complex ``enum`` member, an open
``additionalProperties``), and :func:`count_union_or_type_array_nodes` /
:func:`count_optional_properties` tally the whole tree. The caller composes them and owns
the named reasons; these report generic schema facts and name no grammar.
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


def _portable_keyword(keyword: str, value: Any, path: str, expand_mixed_enum: bool) -> Any:
    """Rewrite one keyword's value, recursing into any schema node(s) it carries."""
    if keyword in _SCHEMA_MAP_VALUED and isinstance(value, dict):
        return {name: _portable(sub, f"{path}.{keyword}.{name}", expand_mixed_enum) for name, sub in value.items()}
    if keyword in _SCHEMA_LIST_VALUED and isinstance(value, list):
        return [_portable(sub, f"{path}.{keyword}[{index}]", expand_mixed_enum) for index, sub in enumerate(value)]
    if keyword in _SCHEMA_VALUED:
        # A boolean additionalProperties/items stays as-is; a schema node recurses.
        if isinstance(value, dict):
            return _portable(value, f"{path}.{keyword}", expand_mixed_enum)
        return copy.deepcopy(value)
    return copy.deepcopy(value)


def _single_type_body(node: dict[str, Any], json_type: str, path: str, expand_mixed_enum: bool) -> dict[str, Any]:
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
            body[keyword] = _portable_keyword(keyword, node[keyword], path, expand_mixed_enum)
    if "enum" in node:
        members = [value for value in node["enum"] if _json_type_of(value) == json_type]
        if members:
            body["enum"] = members
    if "const" in node and _json_type_of(node["const"]) == json_type:
        body["const"] = node["const"]
    return body


def _carry_defs(node: dict[str, Any], out: dict[str, Any], path: str, expand_mixed_enum: bool) -> None:
    """Recurse and carry a ``$defs`` block (and a ``$ref``) onto the rewritten ``out`` node."""
    if "$defs" in node and isinstance(node["$defs"], dict):
        out["$defs"] = {
            name: _portable(sub, f"{path}.$defs.{name}", expand_mixed_enum) for name, sub in node["$defs"].items()
        }
    if "$ref" in node:
        out["$ref"] = node["$ref"]


def _annotations(node: dict[str, Any]) -> dict[str, Any]:
    return {keyword: copy.deepcopy(node[keyword]) for keyword in _ANNOTATION_KEYWORDS if keyword in node}


def _portable_combinator(node: dict[str, Any], path: str, expand_mixed_enum: bool) -> dict[str, Any]:
    """The rewritten form of a node whose structure is an ``anyOf``/``oneOf``/``allOf``."""
    out = _annotations(node)
    for present in ("anyOf", "oneOf", "allOf"):
        if present in node:
            out[present] = [
                _portable(sub, f"{path}.{present}[{index}]", expand_mixed_enum)
                for index, sub in enumerate(node[present])
            ]
    _carry_defs(node, out, path, expand_mixed_enum)
    return out


def _portable_typeless(node: dict[str, Any], path: str, expand_mixed_enum: bool) -> dict[str, Any]:
    """The rewritten form of a type-less node, inferring the type from ``const``/``enum``.

    A ``const`` or a single-type ``enum`` yields the inferred ``type``. A type-less
    ``enum`` whose members span more than one JSON type is expanded into an ``anyOf``
    of single-type members when ``expand_mixed_enum`` is set, and otherwise LEFT
    UNCHANGED (bare — reported non-representable rather than inflated). A node with
    nothing to infer a type from is left unchanged when ``expand_mixed_enum`` is not
    set, and otherwise raises :class:`NonPortableSchemaError`.
    """
    if "const" in node:
        out = {**_annotations(node), **_single_type_body(node, _json_type_of(node["const"]), path, expand_mixed_enum)}
        out["enum"] = [node["const"]]
        _carry_defs(node, out, path, expand_mixed_enum)
        return out
    if "enum" in node:
        types = sorted({_json_type_of(value) for value in node["enum"]})
        if len(types) == 1:
            out = {**_annotations(node), **_single_type_body(node, types[0], path, expand_mixed_enum)}
        elif expand_mixed_enum:
            out = _annotations(node)
            out["anyOf"] = [_single_type_body(node, member_type, path, expand_mixed_enum) for member_type in types]
        else:
            return copy.deepcopy(node)
        _carry_defs(node, out, path, expand_mixed_enum)
        return out
    if expand_mixed_enum:
        raise NonPortableSchemaError(path)
    return copy.deepcopy(node)


def _portable(node: Any, path: str, expand_mixed_enum: bool) -> Any:
    """Rewrite one schema node (recursively), expanding mixed type-less enums per ``expand_mixed_enum``."""
    if not isinstance(node, dict):
        # A boolean schema (True/False) or a non-dict leaf is already in the native subset.
        return copy.deepcopy(node)

    if any(combinator in node for combinator in ("anyOf", "oneOf", "allOf")):
        return _portable_combinator(node, path, expand_mixed_enum)

    if "$ref" in node:
        out = _annotations(node)
        _carry_defs(node, out, path, expand_mixed_enum)
        return out

    json_type = node.get("type")

    if json_type is None:
        return _portable_typeless(node, path, expand_mixed_enum)

    if isinstance(json_type, list):
        out = _annotations(node)
        members = json_type
        if "enum" in node or "const" in node:
            # An ``enum``/``const`` alongside the type array restricts the instances to those
            # values; a declared type with no value of that type among them accepts nothing, so
            # it contributes no anyOf member (dropping it keeps the accepted set unchanged — a
            # branch with no partitioned values would otherwise accept any instance of that type).
            present = {_json_type_of(value) for value in node.get("enum", [])}
            if "const" in node:
                present.add(_json_type_of(node["const"]))
            members = [member for member in json_type if member in present]
        out["anyOf"] = [_single_type_body(node, member, f"{path}|{member}", expand_mixed_enum) for member in members]
        _carry_defs(node, out, path, expand_mixed_enum)
        return out

    out = {**_annotations(node), **_single_type_body(node, json_type, path, expand_mixed_enum)}
    _carry_defs(node, out, path, expand_mixed_enum)
    return out


def to_portable_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return ``schema`` rewritten into the native-portable subset, without changing what it validates.

    A nullable ``"type"`` array becomes an ``anyOf`` of single-type members (each
    keeping only the keywords that apply to its type); a type-less ``enum``/``const``
    node has its type inferred from the member(s), a mixed type-less ``enum`` expanding
    into an ``anyOf`` of single-type members; recursion runs through ``properties``,
    ``items``, ``prefixItems``, ``anyOf``/``oneOf``/``allOf``, ``additionalProperties``
    schemas and ``$defs``. A node with no ``type`` and nothing to infer one from raises
    :class:`NonPortableSchemaError`.
    """
    return _portable(schema, "$", expand_mixed_enum=True)


def adapt_native_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return ``schema`` with only the minimal, value-preserving native-grammar adaptations.

    A nullable/multi ``"type"`` array becomes an ``anyOf`` of single-type members; a
    type-less ``enum``/``const`` whose members are all one JSON type gets that ``type``.
    A type-less ``enum`` whose members span more than one JSON type is LEFT UNCHANGED
    (bare): no value-preserving minimal adaptation gives it a single type, so it is left
    for :func:`check_native_representable` to report rather than inflated into an
    ``anyOf`` union. Recursion runs through ``properties``, ``items``, ``prefixItems``,
    ``anyOf``/``oneOf``/``allOf``, ``additionalProperties`` schemas and ``$defs``, so a
    mixed enum nested anywhere is left bare. The set of instances the schema accepts is
    unchanged.
    """
    return _portable(schema, "$", expand_mixed_enum=False)


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


def _is_bare_typeless(node: dict[str, Any]) -> bool:
    """Whether ``node`` carries no concrete carrier a native grammar can bind.

    A native grammar needs a ``type``, a combinator (``anyOf``/``oneOf``/``allOf``) or
    a ``$ref`` on every node. A node with none of these — such as a mixed type-less
    ``enum`` left bare by :func:`adapt_native_schema` — has nothing to bind.
    """
    return not ("type" in node or "anyOf" in node or "oneOf" in node or "allOf" in node or "$ref" in node)


def _nonrepresentable_reason(node: Any, path: str) -> str | None:
    """A human description of the first node a native grammar cannot carry, or ``None``."""
    if not isinstance(node, dict):
        return None
    if isinstance(node.get("additionalProperties"), dict):
        # A map object (schema-valued additionalProperties): every native grammar closes objects.
        return f"a map object (schema-valued additionalProperties) at {path}.additionalProperties"
    if _is_bare_typeless(node):
        if "enum" in node:
            return f"a type-less enum at {path}"
        if "const" in node:
            return f"a type-less const at {path}"
        return f"a type-less schema node at {path}"
    for sub, sub_path in _child_schemas(node, path):
        found = _nonrepresentable_reason(sub, sub_path)
        if found is not None:
            return found
    return None


def check_native_representable(schema: dict[str, Any]) -> bool:
    """Whether ``schema`` (as it will be sent) is one the provider-native grammars can carry.

    ``False`` when the schema contains a map object (a schema-valued
    ``additionalProperties``) — the native grammars close every object — or a bare
    type-less node (no ``type``, no combinator and no ``$ref``, such as a mixed
    type-less ``enum`` left bare by :func:`adapt_native_schema`), which a native
    grammar has no concrete carrier for. In either case the plan must fall back to the
    tool tier (or refuse). ``additionalProperties`` that is a boolean, or absent, is
    representable.
    """
    return _nonrepresentable_reason(schema, "$") is None


def native_representability_reason(schema: dict[str, Any]) -> str | None:
    """A human description of why ``schema`` is not natively representable, or ``None`` if it is.

    Names the first offending construct and its JSON path (e.g. ``"a type-less enum at
    $.properties.talk.items.properties.id"``) for a loud caller-facing error. Vendor-free.
    """
    return _nonrepresentable_reason(schema, "$")


def iter_schema_nodes(schema: Any, path: str = "$") -> Iterator[tuple[dict[str, Any], str]]:
    """Yield every schema node (each ``dict``) in ``schema`` with its JSON path, document order.

    Walks the standard schema-valued positions — ``properties``, ``patternProperties``,
    ``$defs``, ``prefixItems``, ``anyOf``/``oneOf``/``allOf``, ``items``, ``contains``,
    ``propertyNames`` and ``additionalProperties`` (when a schema node) — without following
    ``$ref`` (a reference target is reached through its own definition), so a reference cycle
    never loops the walk. A non-dict value yields nothing.
    """
    if not isinstance(schema, dict):
        return
    yield schema, path
    for keyword in _SCHEMA_MAP_VALUED:
        mapping = schema.get(keyword)
        if isinstance(mapping, dict):
            for name, sub in mapping.items():
                yield from iter_schema_nodes(sub, f"{path}.{keyword}.{name}")
    for keyword in (*_SCHEMA_LIST_VALUED, "anyOf", "oneOf", "allOf"):
        members = schema.get(keyword)
        if isinstance(members, list):
            for index, sub in enumerate(members):
                yield from iter_schema_nodes(sub, f"{path}.{keyword}[{index}]")
    for keyword in _SCHEMA_VALUED:
        sub = schema.get(keyword)
        if isinstance(sub, dict):
            yield from iter_schema_nodes(sub, f"{path}.{keyword}")


def has_external_ref(node: dict[str, Any]) -> bool:
    """Whether ``node`` carries a ``$ref`` whose target is not a local pointer (not ``#...``)."""
    ref = node.get("$ref")
    return isinstance(ref, str) and not ref.startswith("#")


def has_ref_with_allof(node: dict[str, Any]) -> bool:
    """Whether ``node`` combines ``allOf`` with a sibling ``$ref`` in the same node."""
    return "allOf" in node and "$ref" in node


def has_numeric_constraint(node: dict[str, Any]) -> bool:
    """Whether ``node`` carries any numeric range or step constraint keyword."""
    return any(keyword in node for keyword in _NUMBER_KEYWORDS)


def has_string_length_or_pattern(node: dict[str, Any]) -> bool:
    """Whether ``node`` carries a string length or ``pattern`` constraint.

    A string ``format`` is an annotation, not a length/pattern constraint, and is not
    reported here.
    """
    return "minLength" in node or "maxLength" in node or "pattern" in node


def has_array_constraint(node: dict[str, Any]) -> bool:
    """Whether ``node`` carries an array size or content constraint beyond a ``minItems`` of 0 or 1.

    ``maxItems``, ``uniqueItems``, ``contains``/``minContains``/``maxContains`` and a
    ``minItems`` of 2 or more are constraints; a ``minItems`` of 0 or 1 is the trivial /
    singleton bound and is not.
    """
    if any(keyword in node for keyword in ("maxItems", "uniqueItems", "contains", "minContains", "maxContains")):
        return True
    min_items = node.get("minItems")
    return isinstance(min_items, int) and not isinstance(min_items, bool) and min_items > 1


def has_complex_enum_member(node: dict[str, Any]) -> bool:
    """Whether ``node``'s ``enum`` holds a member that is a complex type (an object or an array)."""
    enum = node.get("enum")
    if not isinstance(enum, list):
        return False
    return any(isinstance(member, (dict, list)) for member in enum)


def has_open_additional_properties(node: dict[str, Any]) -> bool:
    """Whether ``node`` is an object that does not close itself with ``additionalProperties: false``.

    An object node — one whose ``type`` is ``"object"`` or that carries ``properties`` — is
    reported unless its ``additionalProperties`` is exactly ``False`` (an omitted, a ``True`` or
    a schema-valued ``additionalProperties`` all report). A non-object node is never reported.
    """
    if node.get("type") != "object" and "properties" not in node:
        return False
    return node.get("additionalProperties") is not False


def count_union_or_type_array_nodes(schema: dict[str, Any]) -> int:
    """How many schema nodes in the whole tree carry an ``anyOf`` or a list-valued ``type``.

    Each node is counted once (a node with both an ``anyOf`` and a type array counts once).
    """
    return sum(1 for node, _ in iter_schema_nodes(schema) if "anyOf" in node or isinstance(node.get("type"), list))


def count_optional_properties(schema: dict[str, Any]) -> int:
    """How many declared properties in the whole tree are absent from their object's ``required``."""
    total = 0
    for node, _ in iter_schema_nodes(schema):
        properties = node.get("properties")
        if isinstance(properties, dict):
            required = node.get("required")
            required_names = set(required) if isinstance(required, list) else set()
            total += sum(1 for name in properties if name not in required_names)
    return total


def _resolve_local_ref(root: dict[str, Any], ref: str) -> tuple[Any, str] | None:
    """Resolve a local ``$ref`` (``#/...``) against ``root`` to its ``(node, JSON path)``, or ``None``.

    Follows the JSON-pointer tokens (unescaping ``~1``/``~0``) through object keys and array
    indices. A pointer that does not resolve to a present node returns ``None``.
    """
    if not ref.startswith("#"):
        return None
    pointer = ref[1:].lstrip("/")
    if pointer == "":
        return root, "$"
    node: Any = root
    doc_path = "$"
    for raw_token in pointer.split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
            doc_path = f"{doc_path}.{token}"
        elif isinstance(node, list):
            try:
                index = int(token)
            except ValueError:
                return None
            if not 0 <= index < len(node):
                return None
            node = node[index]
            doc_path = f"{doc_path}[{index}]"
        else:
            return None
    return node, doc_path


def _local_refs_in(node: dict[str, Any]) -> Iterator[str]:
    """Every local ``$ref`` string found in ``node`` and its subtree (without following refs)."""
    for sub, _ in iter_schema_nodes(node):
        ref = sub.get("$ref")
        if isinstance(ref, str) and ref.startswith("#"):
            yield ref


def find_ref_cycle(schema: dict[str, Any]) -> str | None:
    """The JSON path of a reference target reached through a ``$ref`` cycle, or ``None``.

    Resolves each local ``$ref`` (``#/...``) against ``schema`` and reports the path of the
    first target reachable from itself — a recursive schema. External references (not
    ``#/...``) are not followed.
    """

    def follow(ref: str, seen: frozenset[str]) -> str | None:
        resolved = _resolve_local_ref(schema, ref)
        if resolved is None:
            return None
        target, target_path = resolved
        if ref in seen:
            return target_path
        if not isinstance(target, dict):
            return None
        for child_ref in _local_refs_in(target):
            found = follow(child_ref, seen | {ref})
            if found is not None:
                return found
        return None

    for ref in _local_refs_in(schema):
        found = follow(ref, frozenset())
        if found is not None:
            return found
    return None
