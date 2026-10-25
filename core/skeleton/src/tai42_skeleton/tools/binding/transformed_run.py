"""Running a transformed tool in process, and the typed runner an extension branch over one wraps."""

from __future__ import annotations

import inspect
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any, cast

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool
from fastmcp.tools.tool_transform import TransformedTool
from makefun import create_function
from tai42_kit.utils.data import makefun_func_name
from tai42_kit.utils.unset import without_unset

from tai42_skeleton.tools.binding.result import _tool_result_value
from tai42_skeleton.tools.binding.schema import _resolved_signature, schema_signature
from tai42_skeleton.tools.context_bridge import bridge_context
from tai42_skeleton.tools.reveal_gate import InprocessRevealGate, inprocess_reveal_gate


class SchemaInputTool(TransformedTool):
    """A transformed tool whose ``parameters`` is an authored object schema its own transform validates.

    Its input is not the parent's arguments, so no signature derived from the parent describes it.
    """


async def run_transformed_tool(fastmcp: FastMCP, tool: TransformedTool, arguments: Mapping[str, Any]) -> Any:
    """Run ``tool`` through its own ``run`` in process and return the value an in-process caller receives.

    The transform validates the arguments, applies its hidden constants and refuses a caller that
    passes one. The in-process reveal gate is armed for the run, so a secret-bearing return comes
    back wrapper-intact and a park comes back as the park object itself; any other result is
    reduced by :func:`~tai42_skeleton.tools.binding.result._tool_result_value`. A fresh gate per
    call: a failed attempt's gate state never leaks into the next.
    """
    gate = InprocessRevealGate()
    token = inprocess_reveal_gate.set(gate)
    try:
        with bridge_context(fastmcp):
            result = await tool.run(dict(arguments))
    finally:
        inprocess_reveal_gate.reset(token)
    if gate.has_park:
        return gate.park
    if gate.has_payload:
        return gate.payload
    return _tool_result_value(result)


def _runner_signature(tool: TransformedTool) -> tuple[inspect.Signature, str | None]:
    """The signature ``tool``'s runner presents, and its docstring.

    A :class:`SchemaInputTool` presents its authored schema. Any other transformed tool takes its
    parent function's own arguments, so it presents that function's resolved signature kept to
    the names its ``parameters`` lists: its hidden arguments and every injected parameter are left
    to the transform.
    """
    if isinstance(tool, SchemaInputTool):
        return schema_signature(tool.parameters), tool.description
    parent = tool.parent_tool
    if not isinstance(parent, FunctionTool):
        raise TypeError(f"transformed tool {tool.name!r} has no function base to branch")
    properties = tool.parameters.get("properties", {})
    full = _resolved_signature(parent.fn)
    unknown = sorted(set(properties) - set(full.parameters))
    if unknown:
        raise TypeError(
            f"cannot branch-bind {tool.name!r}: argument(s) {', '.join(unknown)} are not parameters of its base"
        )
    kept = [param for param in full.parameters.values() if param.name in properties]
    return full.replace(parameters=kept, return_annotation=inspect.Signature.empty), parent.fn.__doc__


def _transformed_tool_runner(tool: TransformedTool, fastmcp: Callable[[], FastMCP]) -> Callable[..., Any]:
    """An async function presenting ``tool``'s arguments whose body runs ``tool`` in process.

    The supplied arguments, bound through the presented signature and with every omitted-value
    marker dropped, go to :func:`run_transformed_tool`; the transform does the rest.
    """
    signature, doc = _runner_signature(tool)

    async def run(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        return await run_transformed_tool(fastmcp(), tool, without_unset(bound.arguments))

    # makefun's ``doc`` is annotated ``str`` but accepts ``None`` (its own default).
    return create_function(
        signature,
        cast(Callable[[Any], Any], run),
        func_name=makefun_func_name(tool.name),
        doc=cast(str, doc),
    )


# One runner per tool object, so a repeated resolve yields the SAME callable and fastmcp's
# identity-keyed caches still hit. Keyed on ``id`` (a fastmcp ``Tool`` is unhashable): safe only
# because the runner strongly references its tool, pinning that id for the entry's life. Dropped
# when the serving generation advances, so it holds only the live epoch's tools.
_RUNNER_CACHE_MAX = 2048
_runner_cache: OrderedDict[int, Callable[..., Any]] = OrderedDict()
_runner_cache_epoch: int | None = None


def transformed_tool_runner(tool: TransformedTool, fastmcp: Callable[[], FastMCP]) -> Callable[..., Any]:
    """The cached :func:`_transformed_tool_runner` of ``tool``; ``fastmcp`` yields the live server at call time."""
    from tai42_skeleton.app.epoch import current_epoch_or_none

    global _runner_cache_epoch
    epoch = current_epoch_or_none()
    number = epoch.number if epoch is not None else None
    if number != _runner_cache_epoch:
        _runner_cache.clear()
        _runner_cache_epoch = number
    key = id(tool)
    cached = _runner_cache.get(key)
    if cached is not None:
        _runner_cache.move_to_end(key)
        return cached
    runner = _transformed_tool_runner(tool, fastmcp)
    _runner_cache[key] = runner
    if len(_runner_cache) > _RUNNER_CACHE_MAX:
        _runner_cache.popitem(last=False)
    return runner
