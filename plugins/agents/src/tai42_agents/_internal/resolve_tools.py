"""Resolve an agent's declared tool inputs into a flat ``StructuredTool`` list.

The three inputs — ``tools`` (live objects), ``tool_names`` (resolved through the
app's tool registry), and ``presets`` (a base tool bound to fixed kwargs) — all
resolve here.

A preset becomes a ``StructuredTool`` whose LLM-visible arguments are the base
tool's arguments minus the fixed ones (so the agent cannot override a fixed
value); its bound callable invokes the base tool via ``app_tools.run_tool`` with
the fixed kwargs merged under the caller's runtime kwargs.

Every resolved tool is scoped through
:func:`~tai42_agents._internal.nested_dispatch.scope_nested_dispatch_all`: this is the
one list an agent dispatches from, so it is where the ownership rule is enforced —
a tool called INSIDE an agent turn cannot capture the completion binding that
addresses the agent's own deferred answer.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel
from tai42_contract.agent.base import PresetSpec
from tai42_contract.interactions import (
    NestedParkOwnershipError,
    SuspendedInteraction,
    assert_park_adoptable,
    suspended_interaction_marker,
)
from tai42_contract.secrets import mask_secrets
from tai42_contract.tools import AppTools

from tai42_agents._internal.nested_dispatch import scope_nested_dispatch_all


def _assert_unique_names(tools: list[StructuredTool]) -> None:
    """Reject duplicate tool names — the agent dispatches by name, so a collision
    would make selection ambiguous."""
    names = [tool.name for tool in tools]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(
            f"agent has duplicate tool names across tools/tool_names/presets: {duplicates}. Tool names must be unique."
        )


async def _as_structured_tool(
    app_tools: AppTools,
    preset: PresetSpec,
) -> StructuredTool:
    """Bind ``preset`` into a ``StructuredTool``.

    The base tool's argument schema, minus the preset's fixed keys, becomes the
    new tool's argument schema; the bound callable merges the runtime args over
    the fixed kwargs and invokes the base tool via ``app_tools.run_tool``.
    """

    async def run_impl(**runtime: Any) -> Any:
        # A plain-dict args_schema does not strip out-of-schema keys, so drop the
        # fixed keys here to keep the bound values immutable on the merge below.
        runtime = {key: value for key, value in runtime.items() if key not in preset.fixed_kwargs}
        result = await app_tools.run_tool(preset.base_tool, {**preset.fixed_kwargs, **runtime})
        if isinstance(result, SuspendedInteraction):
            # The base tool async-parked and returned this sentinel through the direct-run
            # seam (preserved by type, never flattened). This coroutine is a plain langchain
            # tool, so — exactly as the direct client-tool langchain adapter does — convert the
            # sentinel to the reserved contract park marker: the ToolMessage commits carrying
            # it and the in-graph park middleware recognizes the park by RESULT shape (never a
            # tool name), so a preset over ANY parking tool parks the agent. GENERIC.
            #
            # Adopting the park is what suspends THIS run, so it is refused unless this run
            # owns the park: a preset over a base that drives a nested run (a flow, another
            # agent) surfaces a park resumed on that run's own path, which would never resume
            # this one. Re-raised as a ``ToolException`` — the ONE exception class the loop's
            # error middleware turns into a model-visible error ``ToolMessage`` — so the model
            # reads the refusal and finishes the turn around it, instead of the turn aborting
            # (or hanging on a park nothing will resume).
            try:
                assert_park_adoptable(result.resume_owner, interaction_id=result.interaction_id, tool_name=preset.name)
            except NestedParkOwnershipError as exc:
                raise ToolException(str(exc)) from exc
            # The owner rides the WIRE form too: the claim point reads it off the serialized
            # ToolMessage, never off the sentinel this seam holds.
            return suspended_interaction_marker(result.interaction_id, result.expiry_at, result.resume_owner)
        # This coroutine is the tool adapter this plugin registers with langchain:
        # langchain turns whatever it returns into the model-visible ToolMessage
        # (checkpoint and callback trace included). Mask here so the model never
        # sees a SecretValue -- code that needs the real secret calls the tool
        # outside an agent turn.
        return mask_secrets(result)

    base_tool = (await app_tools.get_client_tools([preset.base_tool]))[0]
    # ``args_schema`` is a JSON-schema dict, a pydantic model class, or None. Read its
    # ``required`` in the same namespace ``base_tool.args`` keys, so filtering against
    # ``props`` below never downgrades a mandatory field.
    base_schema = base_tool.args_schema
    if base_schema is None:
        base_required: list[str] = []
    elif isinstance(base_schema, dict):
        base_required = base_schema.get("required", [])
    elif isinstance(base_schema, type) and issubclass(base_schema, BaseModel):
        # ``args`` keys a model field by field name, so read ``required`` by field name.
        base_required = base_schema.model_json_schema(by_alias=False).get("required", [])
    else:
        # A class reaching here (e.g. a pydantic-v1 model) is named by its own
        # ``__name__``; ``type(...).__name__`` would report its metaclass instead.
        # Any other value is named by its type — a function or module also carries
        # a ``__name__``, which names the value, not the type that was rejected.
        offender = base_schema.__name__ if isinstance(base_schema, type) else type(base_schema).__name__
        raise TypeError(
            f"preset base tool {preset.base_tool!r} exposes an unsupported args_schema of type "
            f"{offender}; expected a JSON-schema dict, a pydantic model class, or None"
        )
    # Accept a list or tuple of names; reject any other shape rather than iterating
    # it (a bare string would be walked char by char, matching no property and
    # silently downgrading a mandatory argument to optional).
    if not isinstance(base_required, (list, tuple)) or not all(isinstance(name, str) for name in base_required):
        raise TypeError(
            f"preset base tool {preset.base_tool!r} declares a malformed args_schema 'required': "
            f"expected a list or tuple of property names, got {base_required!r}"
        )
    # Filter ``required`` against the surviving properties so a fixed key (or a
    # required field ``args`` omits) cannot slip into the exposed ``required`` list.
    props = {key: value for key, value in base_tool.args.items() if key not in preset.fixed_kwargs}
    required = [name for name in base_required if name in props]
    args_schema = {"type": "object", "properties": props, "required": required}

    return StructuredTool.from_function(
        func=None,
        coroutine=run_impl,
        name=preset.name,
        description=preset.description,
        args_schema=args_schema,
    )


async def resolve_tools(
    app_tools: AppTools,
    tool_names: list[str],
    tools: list[StructuredTool],
    presets: list[PresetSpec],
) -> list[StructuredTool]:
    """Resolve ``tools`` + ``tool_names`` + ``presets`` into one deduplicated
    ``StructuredTool`` list (live tools first, then resolved names, then
    presets). ``app_tools`` is the app's tool facet (``tai42_app.tools``).

    Every tool comes back delivery-scoped: its body runs with the park-completion
    binding CLEARED, so a parking driver reached THROUGH the agent cannot claim the
    agent's own deferred-answer address (see
    :mod:`~tai42_agents._internal.nested_dispatch`). The agent's own park, raised
    outside any tool body, is unaffected."""
    out: list[StructuredTool] = list(tools or [])
    if tool_names:
        out += await app_tools.get_client_tools(list(tool_names))
    for preset in presets or []:
        out.append(await _as_structured_tool(app_tools, preset))
    _assert_unique_names(out)
    return scope_nested_dispatch_all(out)
