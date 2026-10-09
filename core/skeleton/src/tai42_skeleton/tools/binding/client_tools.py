"""Binds registered tools as framework-neutral :class:`~tai42_contract.tools.ClientTool` handles.

An in-process agent engine drives a bound tool through its handle: the handle carries the full
registered name, the description and the argument schema the MCP edge advertises, and its
``invoke`` runs the tool body directly (never the ``run_tool`` seam) with the platform's work done
here — the tier fence, the execution-identity gate, argument validation, the undeclared-park
refusal, the encoding refusal and secret masking. The adaptation to any one engine's tool type
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
from fastmcp.utilities.types import get_cached_typeadapter
from makefun import create_function
from pydantic import ValidationError
from tai42_contract.interactions import ResumeBuffered, RunTerminalFailed, SuspendedInteraction, failed_outcome_text
from tai42_contract.secrets import mask_secrets
from tai42_contract.tools import ClientTool, ClientToolError, UndeclaredPauseError, tool_call_frame
from tai42_kit.llm.models import check_tool_name
from tai42_kit.utils.data import makefun_func_name

from tai42_skeleton.agent.binding import _UNSET
from tai42_skeleton.tools.binding.branch import _BranchBindingMixin
from tai42_skeleton.tools.binding.errors import UnknownToolError
from tai42_skeleton.tools.binding.resolution import _ResolutionMixin
from tai42_skeleton.tools.binding.result import UnencodableLeafError, _serialize_result, find_lone_surrogate
from tai42_skeleton.tools.binding.schema import _VAR_PARAM_KINDS, _resolved_signature
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

    A bare ``*args``/``**kwargs`` passthrough validates nothing of its own, and a synthesized agent
    run tool defaults every optional parameter to the :data:`_UNSET` sentinel so its body sees only
    the keys the caller supplied (its own ``ToolInput`` validates them).
    """
    params = list(signature.parameters.values())
    all_var = bool(params) and all(p.kind in _VAR_PARAM_KINDS for p in params)
    return all_var or any(p.default is _UNSET for p in params)


@lru_cache(maxsize=2048)
def _argument_validator(resolved_fn: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
    """A stable wrapper presenting ``resolved_fn``'s signature that returns the validated arguments.

    Validated through fastmcp's process-global ``get_cached_typeadapter`` over the same resolved
    signature the ``run_tool`` seam validates against, so a call is accepted or refused exactly as
    there. Keyed on ``resolved_fn`` (fastmcp's process-cached ``without_injected_parameters`` wrapper,
    or a preset's cached baked partial), so one wrapper serves every call.
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

        Resolves the tool's typed callable via ``_branch_base_callable`` (``FunctionTool`` →
        ``fn``; a preset ``TransformedTool`` → its baked partial; anything else raises) and strips
        the fastmcp-injected Context/``Depends`` params, so the arguments validate against the real
        user parameters and the injected ones resolve at call time.

        The call opens this tool's own call frame (a PUSH of its name, so an ask it raises records
        it in ``asked_by``), enforces the run-time tier fence through the SAME ``enforce_run_tier``
        the ``run_tool`` seam uses, and gates on the execution identity under the tool's registered
        name. Under a bound identity the body is re-resolved from the name LIVE, so the registration
        decided about is the one that runs — a handle outlives an agent turn, and a preset re-based
        or deleted mid-turn would otherwise run its stale baked body; a vanished registration fails
        loudly as an unknown tool. The body runs under ``bridge_context`` so an injected
        ``ctx.elicit()`` / ``ctx.sample()`` resolves through the platform bridges.

        The body path is the direct body path by design: no ``run_tool`` seam, no retry policy and
        no dispatch-scope row, because the agent run tool that drives the engine is itself the
        recorded dispatch.
        """
        resolved = without_injected_parameters(self._branch_base_callable(tool_obj))
        resolved_permissive = _is_permissive(inspect.signature(resolved))
        name = tool_obj.name

        async def call(arguments: Mapping[str, Any]) -> Any:
            supplied = {key: value for key, value in arguments.items() if value is not _UNSET}
            with tool_call_frame(name=name):
                await enforce_run_tier(self._app, name)
                target, permissive = resolved, resolved_permissive
                execution_identity = self._bound_execution_identity()
                if execution_identity is not None:
                    target = without_injected_parameters(
                        self._branch_base_callable(await self._resolve_run_target(name))
                    )
                    permissive = _is_permissive(inspect.signature(target))
                    await self._authorize_execution_dispatch(execution_identity, name, supplied)
                with bridge_context(self._app.fastmcp):
                    kwargs = supplied if permissive else _validated_arguments(name, target, supplied)
                    result = await _invoke_tool_body(target, kwargs, name)
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
