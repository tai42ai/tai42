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
``SecretValue`` is not a ``str`` (nor any scalar): a reference baked into a
TYPED-scalar parameter (``token: str``, ``token: str | None``) would be rejected
at that validation, so the resolved value would never reach the tool.
:func:`reveal_typed_scalar_refs` — the second bind step — reveals a top-level
resolved value into its plain form exactly when its base-tool parameter declares a
concrete type; a value baked into a PERMISSIVE parameter (``Any`` / ``object`` —
pydantic passes it through unchanged) stays wrapped, so it is masked wherever the
run is recorded, and a wrapper nested inside a container value is left untouched
(a container parameter validates its leaves loosely or not at all).
"""

from __future__ import annotations

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


# The JSON-schema keywords that declare a parameter's value type. A base-tool
# parameter whose schema carries any of them is validated by pydantic against a
# concrete type — an ``Any`` / ``object`` parameter carries none and pydantic
# passes an arbitrary value (a ``SecretValue`` included) through unchanged.
_TYPE_KEYWORDS = frozenset({"type", "anyOf", "oneOf", "allOf", "$ref", "enum", "const"})


def reveal_typed_scalar_refs(fixed_kwargs: dict[str, Any], base_parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``fixed_kwargs`` with a resolved secret revealed for each TYPED base parameter.

    ``base_parameters`` is the base tool's input JSON schema. A top-level baked value
    that is a :class:`SecretValue` whose parameter declares a concrete type (see
    :data:`_TYPE_KEYWORDS`) is revealed to its plain value, so a ``token: str`` (or
    ``token: str | None``) parameter receives the resolved string that pydantic
    validation would otherwise reject. A value baked into a PERMISSIVE parameter
    (``Any`` / ``object`` — an empty parameter schema) stays wrapped, so it is masked
    wherever the bound run is recorded. Only top-level values are considered: a
    :class:`SecretValue` nested inside a container value is left wrapped — a container
    parameter validates its leaves loosely or not at all, so the wrapper survives to
    the recorder. The input is never mutated — a fresh mapping is returned.
    """
    properties = base_parameters.get("properties", {})
    return {
        key: (value.reveal() if isinstance(value, SecretValue) and _param_is_typed(properties.get(key)) else value)
        for key, value in fixed_kwargs.items()
    }


def _param_is_typed(param_schema: Any) -> bool:
    """Whether a parameter's JSON schema constrains its value to a concrete type.

    ``True`` when the schema declares any type keyword (:data:`_TYPE_KEYWORDS`) —
    pydantic validates the value and rejects a :class:`SecretValue`; ``False`` for a
    permissive (``Any`` / ``object``) or unknown parameter, whose value pydantic
    passes through unchanged.
    """
    if not isinstance(param_schema, Mapping):
        return False
    return any(keyword in param_schema for keyword in _TYPE_KEYWORDS)


__all__ = ["resolve_secret_refs", "reveal_typed_scalar_refs"]
