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

The read views :func:`redact_baked_secrets` and :func:`mask_all_baked_secrets` mask a
baked value a read-scoped principal must not see, walking the base tool's input schema
with the tool-argument schema walker
(:mod:`tai42_skeleton.tools.binding.secret_arguments`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from tai42_contract.secrets import SECRET_PLACEHOLDER, SecretValue
from tai42_kit.utils.data.env_markers import escape_json_pointer_token, is_env_marker, parse_env_marker

from tai42_skeleton.tools.binding.secret_arguments import (
    _candidates,
    _child_array_schema,
    _child_object_schema,
    _is_array_schema,
    _is_object_schema,
)


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
    return {key: _resolve(value, f"/{escape_json_pointer_token(str(key))}") for key, value in fixed_kwargs.items()}


def _resolve(node: Any, pointer: str) -> Any:
    """Rebuild ``node`` resolving every ``!ENV`` scalar leaf; non-string leaves pass through."""
    if isinstance(node, Mapping):
        return {key: _resolve(value, f"{pointer}/{escape_json_pointer_token(str(key))}") for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [_resolve(value, f"{pointer}/{index}") for index, value in enumerate(node)]
    if isinstance(node, str):
        return _resolve_leaf(node, pointer)
    return node


def _resolve_leaf(leaf: str, pointer: str) -> Any:
    """Resolve a single scalar leaf: an ``!ENV`` marker → its value, else the leaf unchanged."""
    if not is_env_marker(leaf):
        return leaf
    ref = parse_env_marker(leaf)
    if ref is None:
        raise ValueError(
            f"preset secret reference at {pointer} is malformed: a fixed_kwargs !ENV value "
            "must be exactly '!ENV ${VAR}' or '!ENV ${VAR:default}' — a single environment "
            "reference with no surrounding text or extra references"
        )
    var = ref.var
    if var in os.environ:
        return SecretValue(os.environ[var])
    if ref.default is not None:
        return ref.default
    raise ValueError(
        f"preset secret reference at {pointer} resolves to no environment variable: "
        f"set {var}, or give the reference a default ('!ENV ${{{var}:...}}')"
    )


def redact_baked_secrets(fixed_kwargs: dict[str, Any], base_parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``fixed_kwargs`` with every baked value under a SECRET-typed leaf masked.

    A read-view transform for the preset read doors (``get_preset`` / ``get_version`` /
    ``list_versions``): the stored body and the bind / execute paths keep the real
    value — only the value handed to a read-scoped principal is masked. ``base_parameters``
    is the base tool's input JSON schema; the walk pairs each baked value with its
    effective schema exactly as
    :func:`~tai42_skeleton.tools.binding.secret_arguments.reveal_for_validation` does
    (``properties`` / ``items`` /
    ``prefixItems`` / ``additionalProperties`` / ``anyOf`` / ``oneOf`` / ``allOf`` / ``$ref``
    resolved within the schema's own ``$defs``) and replaces every value under a SECRET
    string leaf — a pydantic ``SecretStr`` renders ``{"type": "string", "format":
    "password", "writeOnly": true}``, and either marker (``format: password`` or
    ``writeOnly: true``) on any admissible branch marks the leaf secret — with the
    platform mask (:data:`~tai42_contract.secrets.SECRET_PLACEHOLDER`).

    An ``!ENV ${VAR[:default]}`` marker is left verbatim: it is a reference the store
    keeps, never a baked credential, so masking it would hide nothing and lose the
    reference. A non-secret leaf, and a value under a PERMISSIVE (``Any`` / ``object``)
    branch that pydantic never types as secret, pass through unchanged. The input is
    never mutated — a fresh structure is returned.

    This is NOT the backup export: ``versioning/backup.py`` produces a deliberately
    secret-bearing, default-off opaque dump meant to restore verbatim, so it reads the
    store directly and is never routed through this seam — masking there would corrupt a
    restore.
    """
    properties = base_parameters.get("properties") if isinstance(base_parameters, Mapping) else None
    if not isinstance(properties, Mapping):
        properties = {}
    return {
        key: _redact_value(value, properties.get(key), base_parameters, frozenset())
        for key, value in fixed_kwargs.items()
    }


def _redact_value(value: Any, schema: Any, root: Mapping[str, Any], seen: frozenset[str]) -> Any:
    """Mask ``value`` when it sits under a SECRET leaf of ``schema``; descend containers otherwise.

    A marker string is left verbatim at a secret leaf (a reference, not a credential).
    ``root`` is the whole input schema every ``$ref`` resolves within; ``seen`` guards
    ``$ref`` cycles. A permissive or non-matching schema leaves ``value`` untouched.
    """
    candidates = _candidates(schema, root, seen)
    if _leaf_is_secret(candidates):
        if is_env_marker(value):
            return value
        return SECRET_PLACEHOLDER
    if isinstance(value, Mapping):
        objects = [c for c in candidates if _is_object_schema(c)]
        if not objects:
            return value
        return {
            key: _redact_value(child, _child_object_schema(objects, key), root, seen) for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        arrays = [c for c in candidates if _is_array_schema(c)]
        if not arrays:
            return value
        return [
            _redact_value(child, _child_array_schema(arrays, index), root, seen) for index, child in enumerate(value)
        ]
    return value


def _leaf_is_secret(candidates: list[Any]) -> bool:
    """Whether ANY concrete (``$ref``- and union-expanded) branch marks this position secret.

    A single secret branch is enough: ``str | None`` over a ``SecretStr`` (``anyOf``
    of a ``format: password`` branch and a ``null`` branch) is masked, and a value a
    caller could route to a secret branch is never surfaced in the clear.
    """
    return any(_is_secret_schema(cand) for cand in candidates)


def mask_all_baked_secrets(fixed_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Mask every baked leaf (markers kept) — the fail-closed read-view when no base schema is available.

    An unbindable preset (its base tool's plugin absent) offers no input schema to tell
    a secret leaf from a plain config one, so a read masks every baked value rather than
    risk surfacing a baked credential. An ``!ENV ${VAR[:default]}`` marker is a
    reference the store keeps, not a credential, so it is left verbatim. The input is
    never mutated — a fresh structure is returned.
    """
    return {key: _mask_all(value) for key, value in fixed_kwargs.items()}


def _mask_all(value: Any) -> Any:
    """Rebuild ``value`` masking every scalar leaf; an ``!ENV`` marker leaf is kept verbatim."""
    if isinstance(value, Mapping):
        return {key: _mask_all(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_all(child) for child in value]
    if is_env_marker(value):
        return value
    return SECRET_PLACEHOLDER


def _is_secret_schema(schema: Any) -> bool:
    """Whether a concrete schema declares a stored-secret string leaf.

    ``SecretStr`` renders ``format: password`` AND ``writeOnly: true``; either marker
    alone also declares a write-only credential leaf whose baked value a read must mask.
    """
    return isinstance(schema, Mapping) and (schema.get("format") == "password" or schema.get("writeOnly") is True)


__all__ = ["mask_all_baked_secrets", "redact_baked_secrets", "resolve_secret_refs"]
