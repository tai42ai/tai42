"""The one LangChain adapter: platform tools as ``StructuredTool``s for this plugin's graphs.

Every tool an agent of this plugin hands a graph is built here, from one of two sources:

* a :class:`~tai42_contract.tools.ClientTool` handle (a registered tool resolved through
  ``get_client_tools``, or a live handle a caller passed) — :func:`to_structured_tool`;
* a :class:`~tai42_contract.agent.PresetSpec` (a base tool bound to fixed kwargs) —
  :func:`preset_structured_tool`, which keeps the ``run_tool`` path (retry policy, dispatch row,
  encoding refusal).

Both run their whole call inside a CHAINED
:func:`~tai42_agents._internal.nested_dispatch.nested_tool_dispatch`, so the ownership rule holds
for every tool a graph can call: a nested run a tool drives that parks re-enters this loop through
the chain instead of capturing this run's own claim points. Both map a model-visible failure to
``ToolException`` (the class the loop's error middleware turns into an error ``ToolMessage``) and
let every other error leave the call raw — an undeclared park included, which aborts the run rather
than inviting the model to retry a body whose park side effect already happened.

:func:`adapt_tool_result` turns a park a tool returned into the reserved park marker the in-graph
park middleware recognises by result shape: this run's own park, or — under a chained dispatch — a
park on the CALL; a nested run's park with nothing chained is refused to the model.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from langchain_core.tools import StructuredTool, ToolException
from tai42_contract.agent import PresetSpec
from tai42_contract.interactions import (
    NestedParkOwnershipError,
    RunTerminalFailed,
    SuspendedInteraction,
    failed_outcome_text,
    resolve_park_adoption,
    suspended_interaction_marker,
)
from tai42_contract.secrets import mask_secrets
from tai42_contract.tools import AppTools, ClientTool, ClientToolError, UndeclaredPauseError
from tai42_kit.llm.models import check_tool_name

from tai42_agents._internal.nested_dispatch import nested_tool_dispatch

logger = logging.getLogger(__name__)


def adapt_tool_result(tool_name: str, result: Any) -> Any:
    """The value a graph's ``ToolMessage`` carries for ``result``.

    A :class:`SuspendedInteraction` becomes the reserved park marker, keyed and owned by the park
    THIS run records (:func:`~tai42_contract.interactions.resolve_park_adoption`) and carrying both
    id lists and the deadline, so the park middleware partitions caller from user asks and a chained
    park inherits the nested ask's horizon. A nested run's park this run may not adopt or chain is
    refused as a ``ToolException`` worded for the model. Any other value is returned as it is.
    """
    if not isinstance(result, SuspendedInteraction):
        return result
    try:
        key, owner = resolve_park_adoption(
            result.resume_owner, interaction_id=result.interaction_id, tool_name=tool_name
        )
    except NestedParkOwnershipError as exc:
        # A deliberate refusal already worded for the model and naming the tool: re-raised as its
        # own message, with no traceback-bearing WARNING for a decision made on purpose.
        logger.info("tool %r returned a park this run does not own: %s", tool_name, exc)
        raise ToolException(str(exc)) from exc
    return suspended_interaction_marker(
        key,
        result.expiry_at,
        owner,
        interaction_ids=list(result.interaction_ids),
        caller_interaction_ids=list(result.caller_interaction_ids),
    )


def to_structured_tool(handle: ClientTool) -> StructuredTool:
    """``handle`` as a ``StructuredTool`` advertising its own name, description and argument schema.

    The handle validates the arguments (a dict ``args_schema`` is not validated by LangChain), so a
    refusal reaches the model as the handle's ``ClientToolError`` text.
    """

    async def _run(**arguments: Any) -> Any:
        # A fresh chained key per INVOCATION: two calls in one super-step are two calls.
        with nested_tool_dispatch(chain=True):
            try:
                value = await handle.invoke(arguments)
            except ClientToolError as exc:
                raise ToolException(str(exc)) from exc
            return adapt_tool_result(handle.name, value)

    return StructuredTool(
        name=handle.name,
        description=handle.description,
        args_schema=handle.input_schema,
        coroutine=_run,
    )


def _preset_schema(schema: dict[str, Any], preset: PresetSpec) -> dict[str, Any]:
    """The base tool's argument ``schema`` minus the preset's fixed keys.

    A base ``required`` that is not a list of names is refused: iterating a bare string would
    match no property and silently downgrade a mandatory argument to optional.
    """
    base_required = schema.get("required", [])
    if not isinstance(base_required, (list, tuple)) or not all(isinstance(name, str) for name in base_required):
        raise TypeError(
            f"preset base tool {preset.base_tool!r} declares a malformed input schema 'required': "
            f"expected a list of property names, got {base_required!r}"
        )
    properties = {key: value for key, value in schema.get("properties", {}).items() if key not in preset.fixed_kwargs}
    required = [name for name in base_required if name in properties]
    return {"type": "object", "properties": properties, "required": required}


async def preset_structured_tool(app_tools: AppTools, preset: PresetSpec) -> StructuredTool:
    """``preset`` as a ``StructuredTool`` whose call runs its base tool through ``run_tool``.

    The model sees the base tool's arguments minus the fixed ones, so it cannot override a fixed
    value; the call merges the model's arguments over the fixed kwargs. The base tool's own failure
    is this tool's failure, worded with the preset's name (the name the model called).

    The base tool is read for its argument schema only and is never bound, so the bound tool-name
    length holds the preset's name, not the base's.
    """
    base = await app_tools.get_tool(preset.base_tool)

    # The parameter is not named ``runtime``: a graph's tool node injects its own value into a
    # tool function parameter of that name.
    async def _run(**supplied: Any) -> Any:
        with nested_tool_dispatch(chain=True):
            # A dict args_schema does not strip out-of-schema keys, so the fixed keys are dropped
            # here to keep the bound values immutable on the merge below.
            arguments = {key: value for key, value in supplied.items() if key not in preset.fixed_kwargs}
            try:
                result = await app_tools.run_tool(preset.base_tool, {**preset.fixed_kwargs, **arguments})
            except UndeclaredPauseError:
                raise
            except RunTerminalFailed as exc:
                # The whole opaque outcome as JSON text, so the model reads the driver's own surface.
                raise ToolException(failed_outcome_text(exc.outcome)) from exc
            except Exception as exc:
                logger.warning(
                    "preset tool %r (base tool %r) failed: %s", preset.name, preset.base_tool, exc, exc_info=exc
                )
                raise ToolException(f"Error calling tool {preset.name!r}: {exc}") from exc
            if isinstance(result, SuspendedInteraction):
                return adapt_tool_result(preset.name, result)
            # The ToolMessage feeds the model, its checkpoint and its trace: a wrapped secret is
            # masked before it leaves here.
            return mask_secrets(result)

    return StructuredTool(
        name=preset.name,
        description=preset.description,
        args_schema=_preset_schema(base.parameters, preset),
        coroutine=_run,
    )


def _assert_unique_names(tools: Sequence[StructuredTool]) -> None:
    """Reject duplicate tool names — the agent dispatches by name, so a collision makes selection ambiguous."""
    names = [tool.name for tool in tools]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(
            f"agent has duplicate tool names across tools/tool_names/presets: {duplicates}. Tool names must be unique."
        )


def structured_tools(tools: Sequence[ClientTool]) -> list[StructuredTool]:
    """``tools`` adapted, each name held to the bound tool-name length."""
    out = [to_structured_tool(tool) for tool in tools]
    for tool in out:
        check_tool_name(tool.name)
    return out


async def resolve_tools(
    app_tools: AppTools,
    tool_names: Sequence[str],
    tools: Sequence[ClientTool],
    presets: Sequence[PresetSpec],
) -> list[StructuredTool]:
    """Live ``tools``, then the registered ``tool_names``, then ``presets``, as one adapted list.

    Names are unique across the three sources and none is longer than a bound tool name may be.
    """
    out = [to_structured_tool(tool) for tool in tools]
    if tool_names:
        out += [to_structured_tool(handle) for handle in await app_tools.get_client_tools(list(tool_names))]
    for preset in presets:
        out.append(await preset_structured_tool(app_tools, preset))
    _assert_unique_names(out)
    for tool in out:
        check_tool_name(tool.name)
    return out
