"""Enforces a branch extension's input-schema rule by kind (wrapper preserves,
transformer declares its own)."""

import inspect
from collections.abc import Callable
from typing import Any

from tai42_contract.extensions import ExtensionKind

from tai42_skeleton.exceptions.exceptions import TaiValidationError
from tai42_skeleton.tools.binding.schema import _VAR_PARAM_KINDS, _derive_input_schema, _drop_empty_required


def _enforce_extension_schema(
    extension: str,
    kind: ExtensionKind,
    extension_func: Callable[..., Any],
    prev_func: Callable[..., Any],
    curr_func: Callable[..., Any],
    tool: str,
) -> None:
    """Enforce the branch's input-schema rule by kind PROPERTY, never by
    member identity. WRAPPER (``preserves_schema``) must present the layer's
    input schema unchanged; TRANSFORMER (``declares_schema``) must present
    its own concrete schema; BACKEND (neither) has no schema rule — its
    single-strategy cardinality is enforced by ``ExtensionRegistry.validate``,
    and there is no in-place path to guard (every kind branches)."""
    if kind.preserves_schema:
        _enforce_wrapper_schema(extension, extension_func, prev_func, curr_func, tool)
    elif kind.declares_schema:
        _enforce_transformer_schema(extension, curr_func, tool)


def _enforce_wrapper_schema(
    extension: str,
    extension_func: Callable[..., Any],
    prev_func: Callable[..., Any],
    curr_func: Callable[..., Any],
    tool: str,
) -> None:
    baseline = _derive_input_schema(prev_func)
    branch = _derive_input_schema(curr_func)

    # A wrapper may add control kwargs for itself (e.g. cache's ``exp``),
    # declared as a ``reserved_params`` attribute on the factory (it survives
    # registration — the registry decorator returns the factory unchanged).
    reserved = getattr(extension_func, "reserved_params", frozenset())
    baseline_props = baseline.get("properties", {})
    branch_props = branch.get("properties", {})
    for name in reserved:
        if name in baseline_props:
            raise TaiValidationError(
                f"wrapper tool extension '{extension}' declares reserved param '{name}' that already exists "
                f"on the input schema fed to this layer of tool '{tool}'; excluding it would mask a real change"
            )
        # Subtract from BOTH properties and required — a reserved param with
        # no default lands in ``required`` too, and dropping it only from
        # ``properties`` would leave a dangling ``required`` entry.
        branch_props.pop(name, None)
        if "required" in branch:
            branch["required"] = [p for p in branch["required"] if p != name]

    _drop_empty_required(baseline)
    _drop_empty_required(branch)

    if branch != baseline:
        raise TaiValidationError(
            f"wrapper tool extension '{extension}' changed the schema of tool '{tool}'; "
            "wrapper-kind extensions must preserve the input schema exactly"
        )


def _enforce_transformer_schema(extension: str, curr_func: Callable[..., Any], tool: str) -> None:
    params = list(inspect.signature(curr_func).parameters.values())
    # A concrete makefun signature (batch/chain/ask_external) has named
    # params; a bare passthrough presents only ``*args``/``**kwargs``. A
    # zero-arg ``()`` signature is concrete (no VAR params), so it passes.
    if params and all(p.kind in _VAR_PARAM_KINDS for p in params):
        raise TaiValidationError(
            f"transformer tool extension '{extension}' presents a bare (*args, **kwargs) signature for "
            f"tool '{tool}'; transformer-kind extensions must present their own concrete input schema"
        )
