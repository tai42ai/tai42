"""Composing attached fragments into the effective schema.

Each attachment's substituted (and, when the template traces, ``_trace``-stamped) fragment
is placed into a base schema at its path, refusing collisions and overlapping attach paths.
A fragment placed at a path has its ``#`` and ``#/…`` references rebased to that path, so
they keep resolving against the fragment; a fragment attached at the root merges its
``properties``, ``required`` and ``$defs`` into the root and may carry no other validating
root keyword, nor a pointer reference outside its ``properties`` and ``$defs``. The
composed schema is checked by the declaration rules before it is returned.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, unquote

import jsonschema
from jsonschema import Draft202012Validator
from tai42_contract.states.errors import AttachConflictError, SchemaValidationError

from tai42_skeleton.states.schema import _DRAFT_2020_12, _iter_refs, _schema_nodes, _validate_schema
from tai42_skeleton.states.templates.model import StateTemplate
from tai42_skeleton.states.templates.parameters import _iter_marker_names, substitute_parameters
from tai42_skeleton.states.templates.trace import _inject_trace


def compose_effective_schema(
    base_schema: dict[str, Any], attachments: Sequence[tuple[StateTemplate, list[str], Mapping[str, Any]]]
) -> dict[str, Any]:
    """The base schema with each attachment's fragment placed at its path.

    Each attachment is ``(template, path, parameters)``: the template's fragment is
    substituted with ``defaults`` overlaid by ``parameters`` (an unsupplied no-default
    marker is a loud refusal), ``_trace``-stamped when the template traces, and placed at
    ``path`` — creating intermediate ``{"type": "object", "properties": {}}`` levels. At a
    non-empty path every ``#`` / ``#/…`` ``$ref`` in the fragment is rebased to the placement
    pointer (``#/properties/<seg>/…``), so it resolves inside the fragment as it did alone;
    ``#anchor`` refs are left as they are. At the root path the fragment's root ``type`` must
    be ``object``; its ``properties``, ``required`` and ``$defs`` are merged into the base
    root, its annotation keywords are dropped, and any other root keyword is refused, as is a
    ``#`` / ``#/…`` ref outside the fragment's ``properties`` and ``$defs``. An attachment
    path that collides with an existing base property, a root ``$defs`` key that collides with
    the base's, a root type, keyword or ref that cannot be merged, or an attach path
    overlapping another attachment's path, is refused with
    :class:`~tai42_contract.states.errors.AttachConflictError`. A fragment its parameters leave
    malformed is refused with :class:`~tai42_contract.states.errors.SchemaValidationError`
    naming the template. The composed
    schema satisfies the declaration rules (object-rooted, at least one property, a valid
    draft 2020-12 schema, no ``$schema`` naming another dialect, every ``$ref`` local and
    resolvable, no ``$dynamicRef``, no ``$id``
    below the root, unique ``$anchor`` / ``$dynamicAnchor`` names, no cycle of references that
    never descends into the document), raising
    :class:`~tai42_contract.states.errors.SchemaValidationError` otherwise.
    """
    for i, (template_a, path_a, _pa) in enumerate(attachments):
        for template_b, path_b, _pb in attachments[i + 1 :]:
            if _paths_prefix_overlap(path_a, path_b):
                raise AttachConflictError(
                    f"attach of template {template_a.name!r} at {path_a} overlaps attach of "
                    f"template {template_b.name!r} at {path_b}"
                )
    result = copy.deepcopy(base_schema)
    for template, path, parameters in attachments:
        values = {**template.defaults(), **dict(parameters or {})}
        fragment = substitute_parameters(template.schema, values)
        leftover = sorted(set(_iter_marker_names(fragment)))
        if leftover:
            raise AttachConflictError(
                f"attach of template {template.name!r} leaves parameter(s) {leftover} unsupplied at {list(path)}"
            )
        _check_fragment_schema(fragment, template.name, list(path))
        if template.trace.enabled:
            fragment = _inject_trace(fragment)
        _place_fragment(result, list(path), fragment, template.name)
    _validate_schema(result)
    return result


def _check_fragment_schema(fragment: Any, template_name: str, path: list[str]) -> None:
    """Refuse a substituted fragment that is not a valid draft 2020-12 schema, naming the template.

    Parameter values can make a fragment malformed (a list where ``properties`` needs an
    object); the check runs before the fragment is walked, rebased or merged.
    """
    try:
        Draft202012Validator.check_schema(fragment)
    except jsonschema.SchemaError as exc:
        raise SchemaValidationError(
            f"attach of template {template_name!r} at {path}: the fragment with its parameters is not a valid "
            f"JSON Schema (draft 2020-12): {exc.message}"
        ) from exc


def _paths_prefix_overlap(a: list[str], b: list[str]) -> bool:
    """Whether two concrete attachment paths overlap — one is equal to, or a prefix of, the other.

    Attachment paths carry no wildcards.
    """
    n = min(len(a), len(b))
    return a[:n] == b[:n]


# Root keywords of a root-attached fragment that carry no validation: dropped on merge. A root
# ``$schema`` is dropped only when it names draft 2020-12, the state's own dialect; any other
# value is refused (:func:`_refuse_unmergeable_root`).
_ROOT_ANNOTATIONS = frozenset(
    {"title", "description", "$comment", "examples", "default", "deprecated", "readOnly", "writeOnly", "$schema"}
)
# Root keywords a root-attached fragment merges into the base root (its ``type`` must be ``object``).
_ROOT_MERGED = frozenset({"type", "properties", "required", "$defs"})


def _place_fragment(root: dict[str, Any], path: list[str], fragment: dict[str, Any], template_name: str) -> None:
    if not path:
        _merge_root_fragment(root, fragment, template_name)
        return
    fragment = _rebase_refs(fragment, _placement_pointer(path), template_name, path)
    node = root
    for seg in path[:-1]:
        props = node.setdefault("properties", {})
        child = props.get(seg)
        if child is None:
            child = {"type": "object", "properties": {}}
            props[seg] = child
        elif not (isinstance(child, dict) and child.get("type") == "object"):
            raise AttachConflictError(
                f"attach of template {template_name!r} at {path} passes through non-object property {seg!r}"
            )
        node = child
    props = node.setdefault("properties", {})
    last = path[-1]
    if last in props:
        raise AttachConflictError(
            f"attach of template {template_name!r} at {path} collides with existing property {last!r}"
        )
    props[last] = fragment


def _refuse_unmergeable_root(fragment: dict[str, Any], template_name: str) -> None:
    """Refuse a root-attached fragment whose root cannot merge into the state's object root.

    A root ``$schema`` naming any dialect but draft 2020-12 (:class:`SchemaValidationError`,
    the dialect rule a placed fragment meets too); a root ``type`` other than ``object``, a
    validating root keyword outside ``properties`` / ``required`` / ``$defs``, or a pointer ref
    whose target would move to the state's root (:class:`AttachConflictError`).
    """
    if "$schema" in fragment and fragment["$schema"] not in _DRAFT_2020_12:
        raise SchemaValidationError(
            f"attach of template {template_name!r} at the root: $schema {fragment['$schema']!r} is not supported — "
            f"the schema is one draft 2020-12 document"
        )
    if fragment.get("type") != "object":
        raise AttachConflictError(
            f"attach of template {template_name!r} at the root has root type {fragment.get('type')!r}, which cannot "
            f"be merged into the state's object root — attach it at a path"
        )
    for key in fragment:
        if key not in _ROOT_MERGED and key not in _ROOT_ANNOTATIONS:
            raise AttachConflictError(
                f"attach of template {template_name!r} at the root carries {key!r} at its root, which cannot be "
                f"merged into the state's root schema — attach it at a path"
            )
    _refuse_root_relative_refs(fragment, template_name)


def _merge_root_fragment(root: dict[str, Any], fragment: dict[str, Any], template_name: str) -> None:
    """Merge a root-attached fragment's ``properties``, ``required`` and ``$defs`` into ``root``.

    The fragment's root ``type`` must be ``object`` (a parameter can set it otherwise).
    Annotation keywords are dropped; any other root keyword cannot be merged soundly into the
    state's root and is refused, as is a property or ``$defs`` key the root already holds and a
    pointer ref whose target would move to the state's root.
    """
    _refuse_unmergeable_root(fragment, template_name)
    root_props = root.setdefault("properties", {})
    for key, value in fragment.get("properties", {}).items():
        if key in root_props:
            raise AttachConflictError(
                f"attach of template {template_name!r} at the root collides with existing property {key!r}"
            )
        root_props[key] = value
    for req in fragment.get("required", []):
        required = root.setdefault("required", [])
        if req not in required:
            required.append(req)
    fragment_defs = fragment.get("$defs") or {}
    if fragment_defs:
        root_defs = root.setdefault("$defs", {})
        for key, value in fragment_defs.items():
            if key in root_defs:
                raise AttachConflictError(
                    f"attach of template {template_name!r} at the root collides with existing $defs key {key!r}"
                )
            root_defs[key] = value


def _placement_pointer(path: list[str]) -> str:
    """The JSON pointer (URI-fragment form) of the node a fragment is placed at under ``properties``."""
    return "".join("/properties/" + quote(seg.replace("~", "~0").replace("/", "~1"), safe="") for seg in path)


def _rebase_refs(fragment: dict[str, Any], pointer: str, template_name: str, path: list[str]) -> dict[str, Any]:
    """A copy of ``fragment`` with every schema node's ``#`` / ``#/…`` ``$ref`` prefixed by ``pointer``.

    The nodes are the fragment's :func:`_schema_nodes`, resolved against the fragment's own root:
    its subschemas plus every node a pointer ref reaches. A ``$ref``-shaped value elsewhere inside
    ``const``, ``enum``, ``default`` or ``examples`` is data and stays as it is; ``#anchor`` and
    non-local refs are left untouched. A node to rebase that a pointer ref reaches INSIDE a
    ``const`` or ``enum`` value is both data a record must equal and a schema whose ref must
    move, so it is refused with :class:`SchemaValidationError` naming the template and the ref.
    """
    out = copy.deepcopy(fragment)
    nodes = _schema_nodes(out)
    in_data = {id(item) for node in nodes for key in ("const", "enum") if key in node for item in _dicts_in(node[key])}
    for node in nodes:
        ref = node.get("$ref")
        if not (isinstance(ref, str) and (ref == "#" or ref.startswith("#/"))):
            continue
        if id(node) in in_data:
            raise SchemaValidationError(
                f"attach of template {template_name!r} at {path}: $ref {ref!r} sits inside a const or enum value, "
                f"which cannot keep its data once the ref is rebased to the attach path"
            )
        node["$ref"] = "#" + pointer + ref[1:]
    return out


def _dicts_in(value: Any):
    """Yield every object nested anywhere in the JSON ``value``, ``value`` itself included."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _dicts_in(child)
    elif isinstance(value, list):
        for item in value:
            yield from _dicts_in(item)


def _refuse_root_relative_refs(fragment: dict[str, Any], template_name: str) -> None:
    """Refuse a root-attached fragment's pointer ref whose target changes once merged into the state's root.

    A ``#/properties/<key>…`` or ``#/$defs/<key>…`` pointer keeps its target (a colliding key is
    refused by the merge); ``#`` and any other root pointer would name the merged state root.
    """
    for ref in _iter_refs(fragment):
        if not (ref == "#" or ref.startswith("#/")):
            continue
        tokens = unquote(ref[2:]).split("/") if ref != "#" else []
        if len(tokens) >= 2 and tokens[0] in ("properties", "$defs") and tokens[1]:
            continue
        raise AttachConflictError(
            f"attach of template {template_name!r} at the root carries $ref {ref!r}, which cannot keep its meaning "
            f"in the state's root schema — attach it at a path"
        )
