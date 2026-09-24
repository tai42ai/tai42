"""Resolve ``!ENV`` secret references baked into a preset's ``fixed_kwargs``.

A ``fixed_kwargs`` scalar-string leaf written ``!ENV ${VAR}`` (or ``!ENV
${VAR:default}``) is a secret REFERENCE, not a stored credential: the store keeps
the marker verbatim and every read / list / version / export returns it, while
:func:`resolve_secret_refs` — called once at the bind chokepoint — materialises
each reference from the process environment for the in-memory tool only.

A present variable resolves to its value wrapped in
:class:`~tai42_contract.secrets.SecretValue`, so the framework masks it wherever a
bound run is recorded; an absent variable WITH a ``:default`` resolves to that
default text in the CLEAR (an opt-in non-secret config value, never a credential);
an absent REQUIRED variable — a bare ``${VAR}`` — raises loudly naming the variable
and the leaf, never a silent empty or ``"N/A"`` bake. The marker grammar is the
platform's one authority (:data:`~tai42_kit.utils.data.env_markers.ENV_REF`).

The base tool validates its arguments with pydantic before its body runs, and a
``SecretValue`` is not a ``str`` (nor any scalar): a reference baked into a typed
string leaf — a ``token: str`` parameter, a ``list[str]`` element, a typed model
field, a ``dict[str, str]`` value — would be rejected at that validation, so the
resolved value would never reach the tool. :func:`reveal_typed_refs` — the second
bind step — walks the base tool's input JSON schema alongside the baked
``fixed_kwargs``, pairing each value with its effective schema (``properties`` /
``items`` / ``prefixItems`` / ``additionalProperties`` / ``anyOf`` / ``oneOf`` /
``allOf`` / ``$ref`` resolved within the schema's own ``$defs``), and reveals every
``SecretValue`` sitting under a TYPED string leaf.

A ``SecretValue`` is revealed only where pydantic validation would REJECT the
wrapper — that is, where EVERY admissible schema branch pins a concrete type (see
:data:`_LEAF_TYPE_KEYWORDS`). ``str | None`` (``anyOf[{string}, {null}]`` — both
branches typed) is revealed; a union carrying even one PERMISSIVE branch
(``str | Any`` / ``str | object`` → ``anyOf[{string}, {}]``) ACCEPTS the wrapper
unchanged, so the leaf stays wrapped and is masked wherever the bound run is
recorded. The same rule gates a container value: a permissive branch admits the
whole subtree unchanged, so a dict / list under one is left wholly wrapped, never
descended. A schema oddity — an unresolvable or cyclic ``$ref`` — is treated as
permissive here and never raises; the base tool's own validation is the loud step.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from tai42_contract.secrets import SecretValue
from tai42_kit.utils.data.env_markers import ENV_REF

# The prefix of an ``!ENV`` marker string, mirroring the kit marker convention
# (``tai42_kit.utils.data.yaml_util``). A ``fixed_kwargs`` scalar leaf that begins
# with it is a secret reference resolved here.
_ENV_MARKER_PREFIX = "!ENV "


def resolve_secret_refs(fixed_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return ``fixed_kwargs`` with every ``!ENV`` secret-reference leaf resolved.

    Walks every scalar string leaf (through nested dicts and lists). A leaf that is
    not an ``!ENV`` marker — and every non-string value — passes through unchanged,
    so a literal kwarg bakes exactly as authored. A marker leaf resolves per the
    module rules: a present variable → its value wrapped in :class:`SecretValue`; an
    absent variable with a ``:default`` → the default text; an absent required
    variable, or a marker that is not a single ``${VAR[:default]}`` reference → a
    loud :class:`ValueError` naming the variable and the leaf's json-pointer. The
    input is never mutated — a fresh structure is returned.
    """
    return {key: _resolve(value, f"/{_escape(str(key))}") for key, value in fixed_kwargs.items()}


def _resolve(node: Any, pointer: str) -> Any:
    """Rebuild ``node`` resolving every ``!ENV`` scalar leaf; non-string leaves pass through."""
    if isinstance(node, Mapping):
        return {key: _resolve(value, f"{pointer}/{_escape(str(key))}") for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [_resolve(value, f"{pointer}/{index}") for index, value in enumerate(node)]
    if isinstance(node, str):
        return _resolve_leaf(node, pointer)
    return node


def _resolve_leaf(leaf: str, pointer: str) -> Any:
    """Resolve a single scalar leaf: an ``!ENV`` marker → its value, else the leaf unchanged."""
    if not leaf.startswith(_ENV_MARKER_PREFIX):
        return leaf
    expression = leaf[len(_ENV_MARKER_PREFIX) :]
    match = ENV_REF.fullmatch(expression)
    if match is None:
        raise ValueError(
            f"preset secret reference at {pointer} is malformed: a fixed_kwargs !ENV value "
            "must be exactly '!ENV ${VAR}' or '!ENV ${VAR:default}' — a single environment "
            "reference with no surrounding text or extra references"
        )
    var = match.group(1)
    default = match.group(2)
    if var in os.environ:
        return SecretValue(os.environ[var])
    if default is not None:
        return default[1:]
    raise ValueError(
        f"preset secret reference at {pointer} resolves to no environment variable: "
        f"set {var}, or give the reference a default ('!ENV ${{{var}:...}}')"
    )


def _escape(token: str) -> str:
    """RFC 6901 json-pointer token escaping (matching the kit scalar-leaf walk)."""
    return token.replace("~", "~0").replace("/", "~1")


# The JSON-schema keywords that pin a concrete string-leaf type once unions and
# ``$ref``\ s have been expanded. A leaf schema carrying any of them is validated by
# pydantic against a concrete type — an ``Any`` / ``object`` leaf carries none, and
# pydantic passes an arbitrary value (a ``SecretValue`` included) through unchanged.
# ``anyOf`` / ``oneOf`` / ``allOf`` / ``$ref`` are not listed: they are resolved into
# their concrete branches before a leaf's typedness is decided.
_LEAF_TYPE_KEYWORDS = frozenset({"type", "enum", "const"})


def reveal_typed_refs(fixed_kwargs: dict[str, Any], base_parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``fixed_kwargs`` with every ``SecretValue`` under a TYPED string leaf revealed.

    ``base_parameters`` is the base tool's input JSON schema. The walk pairs each baked
    value with its effective schema — top level ``properties[key]``; a dict value's
    child from ``properties[child]`` else ``additionalProperties``; a list value's
    element from ``prefixItems[i]`` else ``items``; a ``$ref`` resolved within the
    schema's own ``$defs`` / ``definitions``; a union (``anyOf`` / ``oneOf`` / ``allOf``)
    through the branches whose shape matches the value — and reveals a
    :class:`SecretValue` only where validation would REJECT the wrapper: where the
    effective schema has candidate branches and EVERY one is typed (see
    :data:`_LEAF_TYPE_KEYWORDS`).

    A union carrying a PERMISSIVE branch (an empty schema, ``Any`` / ``object``)
    accepts the wrapper unchanged, so the leaf — and any container value under such a
    branch — stays wrapped and is masked wherever the bound run is recorded, never
    descended. A non-secret value passes through unchanged. An unresolvable or cyclic
    ``$ref`` is treated as permissive and never raises. The input is never mutated — a
    fresh structure is returned for every descended branch.
    """
    properties = base_parameters.get("properties") if isinstance(base_parameters, Mapping) else None
    if not isinstance(properties, Mapping):
        properties = {}
    return {
        key: _reveal_value(value, properties.get(key), base_parameters, frozenset())
        for key, value in fixed_kwargs.items()
    }


def _reveal_value(value: Any, schema: Any, root: Mapping[str, Any], seen: frozenset[str]) -> Any:
    """Reveal every ``SecretValue`` in ``value`` that sits under a typed leaf of ``schema``.

    ``root`` is the whole input schema (the target every ``$ref`` resolves within);
    ``seen`` is the set of ``$ref`` strings already followed on this branch, guarding
    against cycles. A permissive or non-matching schema leaves ``value`` untouched.
    """
    if isinstance(value, SecretValue):
        return value.reveal() if _wrapper_rejected(_candidates(schema, root, seen)) else value
    if isinstance(value, (Mapping, list, tuple)):
        candidates = _candidates(schema, root, seen)
        # A permissive (untyped) branch accepts the wrapped value unchanged, so the whole
        # subtree survives validation masked: never descend under one.
        if not _wrapper_rejected(candidates):
            return value
        if isinstance(value, Mapping):
            objects = [c for c in candidates if _is_object_schema(c)]
            if not objects:
                return value
            return {
                key: _reveal_value(child, _child_object_schema(objects, key), root, seen)
                for key, child in value.items()
            }
        arrays = [c for c in candidates if _is_array_schema(c)]
        if not arrays:
            return value
        return [
            _reveal_value(child, _child_array_schema(arrays, index), root, seen) for index, child in enumerate(value)
        ]
    return value


def _wrapper_rejected(candidates: list[Any]) -> bool:
    """Whether pydantic validation would REJECT a bare :class:`SecretValue` at this position.

    ``True`` only when there is at least one candidate branch and EVERY branch pins a
    concrete type (a :data:`_LEAF_TYPE_KEYWORDS` keyword) — no branch then admits the
    wrapper, so it must be revealed. A single permissive branch (an empty / ``Any`` /
    ``object`` schema) accepts the wrapper unchanged, so the value stays wrapped and
    masked; an empty candidate set (permissive, unknown, or unresolvable) does too.
    """
    return bool(candidates) and all(_has_type_keyword(cand) for cand in candidates)


def _has_type_keyword(schema: Any) -> bool:
    """Whether a concrete (``$ref``- and union-expanded) schema declares a type keyword."""
    return isinstance(schema, Mapping) and any(keyword in schema for keyword in _LEAF_TYPE_KEYWORDS)


def _candidates(schema: Any, root: Mapping[str, Any], seen: frozenset[str]) -> list[Any]:
    """Resolve ``schema`` to its concrete branches: follow ``$ref``, expand every union.

    A ``$ref`` is resolved within ``root``; ``anyOf`` / ``oneOf`` expand to their
    branches, ``allOf`` merges its branches into one schema. A permissive, unresolvable,
    or cyclic schema yields an empty list (nothing typed, no container to descend).

    Structurally identical branches are collapsed: a child schema built by
    :func:`_one_or_union` re-wraps each level's contributions into a fresh ``anyOf``,
    so without this the candidate set would double at every level of a nested
    union-of-objects; dedup bounds it to the distinct schemas the tree actually holds.
    """
    if not isinstance(schema, Mapping):
        return []
    resolved, seen = _resolve_ref(schema, root, seen)
    if not isinstance(resolved, Mapping):
        return []
    branches: list[Any] = []
    is_union = False
    for keyword in ("anyOf", "oneOf"):
        options = resolved.get(keyword)
        if isinstance(options, list):
            is_union = True
            for option in options:
                branches.extend(_candidates(option, root, seen))
    all_of = resolved.get("allOf")
    if isinstance(all_of, list):
        is_union = True
        branches.extend(_merge_all_of(all_of, root, seen))
    if not is_union:
        branches.append(resolved)
    return _dedup(branches)


def _merge_all_of(all_of: list[Any], root: Mapping[str, Any], seen: frozenset[str]) -> list[Any]:
    """Merge the branches of an ``allOf`` (an intersection) into concrete schemas.

    Single-branch members fold into one accumulator (``properties`` unioned, other
    keys first-wins); a member that itself expands to several branches is carried
    through as-is, so its typed leaves are still considered.
    """
    merged: dict[str, Any] = {}
    merged_properties: dict[str, Any] = {}
    extras: list[Any] = []
    for member in all_of:
        member_candidates = _candidates(member, root, seen)
        if len(member_candidates) == 1 and isinstance(member_candidates[0], Mapping):
            for key, value in member_candidates[0].items():
                if key == "properties" and isinstance(value, Mapping):
                    merged_properties.update(value)
                else:
                    merged.setdefault(key, value)
        else:
            extras.extend(member_candidates)
    if merged_properties:
        merged["properties"] = merged_properties
    return ([merged] if merged else []) + extras


def _resolve_ref(
    schema: Mapping[str, Any], root: Mapping[str, Any], seen: frozenset[str]
) -> tuple[Any, frozenset[str]]:
    """Follow a ``$ref`` chain within ``root``; a cyclic or unresolvable ref → a permissive schema."""
    while isinstance(schema, Mapping) and "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or ref in seen:
            return {}, seen
        target = _lookup_ref(ref, root)
        if not isinstance(target, Mapping):
            return {}, seen
        seen = seen | {ref}
        schema = target
    return schema, seen


def _lookup_ref(ref: str, root: Mapping[str, Any]) -> Any:
    """Navigate a local ``#/...`` JSON-pointer ``$ref`` from ``root``; ``None`` if it does not resolve."""
    if not ref.startswith("#"):
        return None
    node: Any = root
    for token in ref[1:].split("/"):
        if token == "":
            continue
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or token not in node:
            return None
        node = node[token]
    return node


def _is_object_schema(schema: Any) -> bool:
    """Whether a concrete schema describes an object value (worth descending a dict into)."""
    if not isinstance(schema, Mapping):
        return False
    declared = schema.get("type")
    is_object_type = declared == "object" or (isinstance(declared, list) and "object" in declared)
    return is_object_type or "properties" in schema or "additionalProperties" in schema


def _is_array_schema(schema: Any) -> bool:
    """Whether a concrete schema describes an array value (worth descending a list into)."""
    if not isinstance(schema, Mapping):
        return False
    declared = schema.get("type")
    is_array_type = declared == "array" or (isinstance(declared, list) and "array" in declared)
    return is_array_type or "items" in schema or "prefixItems" in schema


def _child_object_schema(candidates: list[Any], key: str) -> Any:
    """The effective schema for a dict child ``key`` across object ``candidates``.

    Each candidate contributes ``properties[key]`` when present, else its
    ``additionalProperties`` schema when that is a schema dict (``True`` / ``False`` /
    absent are permissive and contribute nothing). Several contributions become an
    ``anyOf`` so a leaf typed by any branch is revealed; none → a permissive schema.
    """
    schemas: list[Any] = []
    for candidate in candidates:
        properties = candidate.get("properties")
        if isinstance(properties, Mapping) and key in properties:
            schemas.append(properties[key])
            continue
        additional = candidate.get("additionalProperties")
        if isinstance(additional, Mapping):
            schemas.append(additional)
    return _one_or_union(schemas)


def _child_array_schema(candidates: list[Any], index: int) -> Any:
    """The effective schema for a list element at ``index`` across array ``candidates``.

    Each candidate contributes ``prefixItems[index]`` when present, else its ``items``
    schema (a dict; a legacy list form is indexed too). ``True`` / ``False`` / absent
    are permissive. Several contributions become an ``anyOf``; none → permissive.
    """
    schemas: list[Any] = []
    for candidate in candidates:
        prefix_items = candidate.get("prefixItems")
        if isinstance(prefix_items, list) and index < len(prefix_items):
            schemas.append(prefix_items[index])
            continue
        items = candidate.get("items")
        if isinstance(items, Mapping):
            schemas.append(items)
        elif isinstance(items, list) and index < len(items):
            schemas.append(items[index])
    return _one_or_union(schemas)


def _one_or_union(schemas: list[Any]) -> Any:
    """A single schema as-is, several as an ``anyOf``, none as a permissive empty schema.

    Contributions are deduplicated first so a union whose branches carry the same child
    schema collapses to one, keeping the candidate set bounded across nested levels.
    """
    schemas = _dedup(schemas)
    if not schemas:
        return {}
    if len(schemas) == 1:
        return schemas[0]
    return {"anyOf": schemas}


def _dedup(schemas: list[Any]) -> list[Any]:
    """Drop structurally identical schemas, preserving order (canonical JSON as the key)."""
    seen_keys: set[str] = set()
    unique: list[Any] = []
    for schema in schemas:
        key = json.dumps(schema, sort_keys=True, default=str)
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(schema)
    return unique


__all__ = ["resolve_secret_refs", "reveal_typed_refs"]
