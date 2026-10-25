"""Binds registered tools as framework-neutral :class:`~tai42_contract.tools.ClientTool` handles.

An in-process agent engine drives a bound tool through its handle: the handle carries the full
registered name, the description and the argument schema the MCP edge advertises, and its
``invoke`` runs the tool directly (never the ``run_tool`` seam) with the platform's work done
here — the tier fence, the execution-identity gate, argument validation, the undeclared-park
refusal, the encoding refusal and secret masking. A transformed tool runs its own transform,
which validates its arguments. The adaptation to any one engine's tool type
lives with that engine.
"""

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, cast

from fastmcp.server.dependencies import without_injected_parameters
from fastmcp.tools.base import Tool
from fastmcp.tools.function_tool import FunctionTool
from fastmcp.tools.tool_transform import TransformedTool
from fastmcp.utilities.types import get_cached_typeadapter
from makefun import create_function
from pydantic import ValidationError
from tai42_contract.interactions import ResumeBuffered, RunTerminalFailed, SuspendedInteraction
from tai42_contract.secrets import mask_secrets
from tai42_contract.tools import ClientTool, ClientToolError, UndeclaredPauseError, tool_call_frame
from tai42_kit.interactions.outcome_text import failed_outcome_text
from tai42_kit.llm.models import check_tool_name
from tai42_kit.utils.data import makefun_func_name
from tai42_kit.utils.unset import UNSET, without_unset

from tai42_skeleton.tools.binding.branch import _BranchBindingMixin
from tai42_skeleton.tools.binding.errors import UnknownToolError
from tai42_skeleton.tools.binding.resolution import _ResolutionMixin
from tai42_skeleton.tools.binding.result import UnencodableLeafError, _serialize_result, find_lone_surrogate
from tai42_skeleton.tools.binding.schema import _VAR_PARAM_KINDS, _resolved_signature
from tai42_skeleton.tools.binding.transformed_run import run_transformed_tool
from tai42_skeleton.tools.context_bridge import bridge_context
from tai42_skeleton.tools.tier import enforce_run_tier

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BoundClientTool:
    """A registered tool bound for in-process use; implements :class:`~tai42_contract.tools.ClientTool`."""

    name: str
    description: str
    input_schema: dict[str, Any]
    _call: Callable[[Mapping[str, Any]], Awaitable[Any]] = field(repr=False)

    async def invoke(self, arguments: Mapping[str, Any]) -> Any:
        """Run the tool once with ``arguments``."""
        return await self._call(arguments)


def _is_permissive(signature: inspect.Signature) -> bool:
    """Whether a body takes the supplied keys as they are rather than a validated argument set.

    A bare ``*args``/``**kwargs`` passthrough validates nothing of its own. A synthesized signature
    that defaults its optional parameters to :data:`~tai42_kit.utils.unset.UNSET` (an agent run
    tool's, and a branch runner's over an authored-schema transformed tool) hands its body only the
    keys the caller supplied, and the body validates them (the agent's ``ToolInput``, the
    transformed tool's own transform).
    """
    params = list(signature.parameters.values())
    all_var = bool(params) and all(p.kind in _VAR_PARAM_KINDS for p in params)
    return all_var or any(p.default is UNSET for p in params)


@lru_cache(maxsize=2048)
def _argument_validator(resolved_fn: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
    """A stable wrapper presenting ``resolved_fn``'s signature that returns the validated arguments.

    Validated through fastmcp's process-global ``get_cached_typeadapter`` over the same resolved
    signature the ``run_tool`` seam validates against, so a call is accepted or refused exactly as
    there. Keyed on ``resolved_fn`` (fastmcp's process-cached ``without_injected_parameters`` wrapper),
    so one wrapper serves every call.
    """

    def collect(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    return create_function(
        _resolved_signature(resolved_fn),
        cast(Callable[[Any], Any], collect),
        func_name=makefun_func_name(resolved_fn.__name__),
    )


def _validated_arguments(tool_name: str, target: Callable[..., Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """``arguments`` validated against ``target``'s signature; a refusal is the model-visible error."""
    try:
        return get_cached_typeadapter(_argument_validator(target)).validate_python(arguments)
    except ValidationError as exc:
        raise ClientToolError(tool_name, f"Error calling tool {tool_name!r}: invalid arguments: {exc}") from exc


def _refuse_unencodable_client_result(tool_name: str, result: Any) -> None:
    """Raise a model-visible :class:`ClientToolError` if ``result`` cannot be JSON-encoded.

    The RAW return is reduced FIRST (so a pydantic/dataclass return cannot slip a surrogate
    through un-walked) and walked for a lone UTF-16 surrogate; on a hit the model receives a
    named tool error identifying the tool and the JSON path, never a crash when an engine later
    encodes the value. A leaf the reduction cannot render at all surfaces as
    ``UnencodableLeafError`` carrying that leaf's path — the same named refusal.
    """
    try:
        offending_path = find_lone_surrogate(_serialize_result(result))
    except UnencodableLeafError as exc:
        offending_path = exc.path
    if offending_path is not None:
        raise ClientToolError(
            tool_name, f"tool {tool_name!r} produced a result that cannot be JSON-encoded at {offending_path}"
        )


async def _invoke_tool_body(target: Callable[..., Any], kwargs: dict[str, Any], tool_name: str) -> Any:
    """Invoke the tool BODY and map its OWN failure to a model-visible :class:`ClientToolError`.

    A driver's FAILED terminal RAISES :class:`RunTerminalFailed` carrying the outcome WHOLE: an
    agent loop reads a tool failure as TEXT, so the whole opaque outcome is rendered as JSON text
    (the contract helper, reading no key inside it, naming no engine). Any other body failure
    becomes the generic named tool error. Only the body's own failure is caught here;
    ``CancelledError``/``BaseException`` pass through untouched, and the surrounding machinery
    (identity gate, validation, park refusal, masking) is never mislabeled as a tool that failed.
    An undeclared park refused by a dispatch inside the body propagates raw, never as a tool error.
    """
    try:
        result = target(**kwargs)
        if inspect.isawaitable(result):
            result = await result
    except UndeclaredPauseError:
        raise
    except RunTerminalFailed as exc:
        raise ClientToolError(tool_name, failed_outcome_text(exc.outcome)) from exc
    except Exception as exc:
        logger.warning("in-process tool %r failed: %s", tool_name, exc, exc_info=exc)
        raise ClientToolError(tool_name, f"Error calling tool {tool_name!r}: {exc}") from exc
    return result


class _ClientToolsMixin(_ResolutionMixin, _BranchBindingMixin):
    """Binds registered tools as framework-neutral handles an in-process agent engine invokes."""

    async def get_client_tools(self, names: list[str] | None = None) -> list[ClientTool]:
        """The framework-neutral handles of the tools named in ``names`` (all when ``None``).

        An unknown name raises :class:`UnknownToolError`; a name longer than a bound tool name may be
        raises ``ToolNameTooLongError`` (never truncated). Each handle advertises the tool's
        ``parameters`` — the schema the MCP edge advertises, injected Context/``Depends`` parameters
        stripped — and its description.
        """
        tools = await self.get_tools()
        for name in names or []:
            if name not in tools:
                raise UnknownToolError(name)
        handles: list[ClientTool] = []
        for name, tool_obj in tools.items():
            if names and name not in names:
                continue
            check_tool_name(name)
            handles.append(
                BoundClientTool(
                    name=name,
                    description=tool_obj.description or "",
                    input_schema=tool_obj.parameters,
                    _call=self._client_call(tool_obj),
                )
            )
        return handles

    def _client_call(self, tool_obj: Tool) -> Callable[[Mapping[str, Any]], Awaitable[Any]]:
        """The call one handle's ``invoke`` runs.

        A ``FunctionTool`` runs its ``fn`` with the fastmcp-injected Context/``Depends`` params
        stripped, so the arguments validate against the real user parameters and the injected
        ones resolve at call time; the body runs under ``bridge_context`` so an injected
        ``ctx.elicit()`` / ``ctx.sample()`` resolves through the platform bridges. A
        ``TransformedTool`` runs through :func:`run_transformed_tool`, and its own transform is the
        one validator of its arguments. Anything else raises loudly.

        The call opens this tool's own call frame (a PUSH of its name, so an ask it raises records
        it in ``asked_by``), enforces the run-time tier fence through the SAME ``enforce_run_tier``
        the ``run_tool`` seam uses, and gates on the execution identity under the tool's registered
        name. Under a bound identity the tool is re-resolved from the name LIVE, so the registration
        decided about is the one that runs — a handle outlives an agent turn, and a preset re-based
        or deleted mid-turn would otherwise run its stale body; a vanished registration fails
        loudly as an unknown tool.

        The body path is the direct body path by design: no ``run_tool`` seam, no retry policy and
        no dispatch-scope row, because the agent run tool that drives the engine is itself the
        recorded dispatch.
        """
        bound_body = self._client_body(tool_obj)
        name = tool_obj.name

        async def call(arguments: Mapping[str, Any]) -> Any:
            supplied = without_unset(arguments)
            with tool_call_frame(name=name):
                await enforce_run_tier(self._app, name)
                body = bound_body
                execution_identity = self._bound_execution_identity()
                if execution_identity is not None:
                    body = self._client_body(await self._resolve_run_target(name))
                    await self._authorize_execution_dispatch(execution_identity, name, supplied)
                result = await body(name, supplied)
                # Only a tool that declares it can pause may return a park signal. Refused RAW,
                # never a ClientToolError: an undeclared park is a registration fault the model
                # must not retry around (a retry would fire the body a second time).
                if isinstance(result, (SuspendedInteraction, ResumeBuffered)):
                    if not await self.pauses(name):
                        raise UndeclaredPauseError(name)
                    return result
                _refuse_unencodable_client_result(name, result)
                # The model never sees a secret value: the engine feeds a result to the model, its
                # checkpoint and its trace, so a wrapped secret is masked before it leaves here.
                return mask_secrets(result)

        return call

    def _client_body(self, tool_obj: Tool) -> Callable[[str, dict[str, Any]], Awaitable[Any]]:
        """The run of ``tool_obj`` over the supplied arguments, its own failure a :class:`ClientToolError`."""
        fastmcp = self._app.fastmcp
        if isinstance(tool_obj, TransformedTool):
            transformed = tool_obj

            async def run_transformed(tool_name: str, supplied: dict[str, Any]) -> Any:
                async def run(**kwargs: Any) -> Any:
                    return await run_transformed_tool(fastmcp, transformed, kwargs)

                return await _invoke_tool_body(run, supplied, tool_name)

            return run_transformed
        if not isinstance(tool_obj, FunctionTool):
            raise TypeError(
                f"cannot bind {type(tool_obj).__name__} {tool_obj.name!r} as a client tool: "
                "expected a FunctionTool or TransformedTool"
            )
        target = without_injected_parameters(tool_obj.fn)
        permissive = _is_permissive(inspect.signature(target))

        async def run_function(tool_name: str, supplied: dict[str, Any]) -> Any:
            with bridge_context(fastmcp):
                kwargs = supplied if permissive else _validated_arguments(tool_name, target, supplied)
                return await _invoke_tool_body(target, kwargs, tool_name)

        return run_function
