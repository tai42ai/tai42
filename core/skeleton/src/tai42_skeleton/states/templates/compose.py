"""Composing attached fragments into the effective schema.

Each attachment's substituted (and, when the template traces, ``_trace``-stamped) fragment
is placed into a base schema at its path, refusing collisions and overlapping attach paths.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from tai42_contract.states.errors import AttachConflictError

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
    ``path`` — creating intermediate ``{"type": "object", "properties": {}}`` levels. An
    attachment path that collides with an existing base property, or that overlaps another
    attachment's path, is refused with
    :class:`~tai42_contract.states.errors.AttachConflictError`."""
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
        if template.trace.enabled:
            fragment = _inject_trace(fragment)
        _place_fragment(result, list(path), fragment, template.name)
    return result


def _paths_prefix_overlap(a: list[str], b: list[str]) -> bool:
    """Whether two concrete attachment paths overlap — one is equal to, or a prefix of, the
    other (attachment paths carry no wildcards)."""
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def _place_fragment(root: dict[str, Any], path: list[str], fragment: dict[str, Any], template_name: str) -> None:
    if not path:
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
        return
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
