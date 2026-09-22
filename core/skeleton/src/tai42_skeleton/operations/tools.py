"""Tool-surface operations — ``/api/tools*`` and ``/api/run-tool``, plus live tool management.

Also exposes the live app-management tools (run, reload, and remove a tool).

Reads:

* ``list_tools`` — the sorted registered tool names.
* ``tool_tags`` — the per-tool native-``tags`` map plus plugin-declared visibility.
* ``tool_schema`` — one tool's input/output/description (unknown name → 404).
* ``tools_schema`` — the same schema view for every tool, keyed by name.

Mutations:

* ``run_tool`` — execute an arbitrary registered tool with REAL side effects, on THIS
  worker only. A "run any tool by name" META-EXECUTOR: hardcode-blocked from the MCP
  surface (``meta_executor=True``, tier 1) and admin-fenced at the HTTP edge; it is
  reachable only as the route + ``tai tools run`` CLI + internal dispatch. Its argument
  is ``tool_name`` (the route's request-model shape).
* ``reload_tool`` — re-register one app tool from its stored definition.
* ``remove_tool`` — remove one app tool from the live registry.

``reload_tool`` / ``remove_tool`` mutate the live registry, so each is applied on this
worker and then broadcast to the fleet over the bus. All three are ``destructive`` and
honor the reload gate.

What each ``run_tool`` branch records: the two 500 branches — an
:class:`~tai42_skeleton.tools.binding.UnknownToolError` naming a DIFFERENT tool than the
one asked for (a dispatch failure inside the running tool's own body) and any other
raise during execution — emit ``logger.exception`` (ERROR, with the caught exception's
traceback); the latter falls back to the exception's CLASS name when the message is
empty, so the envelope always names something. The 404 for a tool that resolved at
lookup and then did not resolve at dispatch emits a ``logger.warning``, so an anomaly
answered as a plain 404 still leaves a server record. Three branches are silent: the 404
for a name that never resolved at all (a caller's own typo, which the response answers),
the typed-``OperationError`` passthrough (the tool's own answer, delivered to the caller
intact), and a RESOLVE that fails for anything OTHER than an unknown tool — that raise
leaves the door untouched, neither enveloped nor logged, so a registry that cannot answer
surfaces as itself.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from tai42_contract.app import tai42_app
from tai42_contract.app.responses import OpaqueJson
from tai42_contract.secrets import unwrap_secrets
from tai42_contract.states import StateSubject

from tai42_skeleton.app.bus import FleetResult
from tai42_skeleton.operations import (
    BadRequestError,
    NotFoundError,
    OperationError,
    OperationFailedError,
    PermissionDeniedError,
    UpstreamError,
    operation,
)
from tai42_skeleton.operations._broadcast import broadcast
from tai42_skeleton.operations.response_models_group_b import (
    StringListResponse,
    ToolSchemaView,
    ToolsSchemaMap,
    ToolTagListResponse,
)
from tai42_skeleton.tools.binding import ToolResultEncodingError, UnknownToolError

if TYPE_CHECKING:
    from fastmcp.tools import Tool
    from tai42_contract.interactions import VisitOutcome

logger = logging.getLogger(__name__)


class RunToolRequest(BaseModel):
    """A synchronous tool-run request: the ``tool_name``, its keyword ``arguments``, and an optional subject.

    Mirrors the shape ``read_tool_call`` enforces at runtime.

    ``subject`` names the addressed subject this run's async parks index under (the door
    deposits it as the run's ``StateContext``); a run whose tool never parks ignores it.
    """

    tool_name: str = Field(min_length=1, description="Registered tool name.")
    arguments: dict[str, object] = Field(default_factory=dict, description="Tool keyword arguments.")
    subject: StateSubject | None = Field(
        default=None, description="The addressed subject an async park of this run indexes under."
    )


class ToolReloadRequest(BaseModel):
    """Re-register or remove one app tool by ``kind`` and ``name``.

    Optionally restricting the fleet fan-out to specific ``targets``.
    """

    kind: str = Field(min_length=1, description='The tool kind (e.g. "example_tool").')
    name: str = Field(min_length=1, description="The tool name.")
    targets: list[str] | None = Field(default=None, description="Workers to restrict the fan-out to.")


def _tool_schema(tool: Tool) -> dict[str, object]:
    """The input/output/description view of a single tool."""
    return {
        "input": tool.parameters,
        "output": tool.output_schema,
        "description": tool.description,
    }


@operation(summary="List the registered tool names", tags=["tools"], response_model=StringListResponse)
async def list_tools() -> list[str]:
    """The sorted registered tool names."""
    tools = await tai42_app.tools.get_tools()
    return sorted(tools.keys())


@operation(
    summary="List each tool's native tags, declared visibility, and badges",
    tags=["tools"],
    response_model=ToolTagListResponse,
)
async def tool_tags() -> list[dict]:
    """The per-tool native-``tags`` map plus the plugin-declared visibility and capability badges.

    One ``{name, tags, hidden, badges}`` entry per registered tool, ``tags`` and ``badges`` each
    sorted for a stable wire order. ``hidden`` is the tool's OWN declaration, read from the
    FastMCP ``meta`` under the namespaced ``tai42/hidden`` key (a tool that never declared it is
    not hidden); ``badges`` is likewise the tool's OWN declared INFORMATIONAL capability badges,
    read from ``meta`` under ``tai42/badges`` (a tool that declared none carries an empty list).
    The tool_meta overlay's tri-state override and its own badge set are merged on top
    client-side; this read exposes only the declaration. Additive to the flat names contract; a
    tool with no tags carries an empty list.
    """
    tools = await tai42_app.tools.get_tools()
    return [
        {
            "name": name,
            "tags": sorted(tool.tags),
            "hidden": (tool.meta or {}).get("tai42/hidden") is True,
            "badges": sorted((tool.meta or {}).get("tai42/badges") or []),
        }
        for name, tool in sorted(tools.items())
    ]


@operation(
    summary="Get one tool's input/output schema",
    tags=["tools"],
    errors=[NotFoundError],
    response_model=ToolSchemaView,
)
async def tool_schema(tool_name: str) -> dict:
    """One tool's input/output/description view (unknown name → 404)."""
    tools = await tai42_app.tools.get_tools()
    tool = tools.get(tool_name)
    if tool is None:
        raise NotFoundError(f"Tool {tool_name!r} not registered")
    return _tool_schema(tool)


@operation(summary="Get the input/output schema of every tool", tags=["tools"], response_model=ToolsSchemaMap)
async def tools_schema() -> dict:
    """The input/output/description view of every registered tool, keyed by name."""
    tools = await tai42_app.tools.get_tools()
    # A schema view needs no callable body, so every registered tool's schema is served
    # here — identical to the per-tool route, which 404s only an unknown name and serves
    # any registered tool's schema. A tool whose ``fn`` is ``None`` is a registry-health
    # signal (a registered name with no backing implementation), so it is logged once
    # per response, never silently dropped.
    missing_impl = [name for name, tool in tools.items() if getattr(tool, "fn", None) is None]
    if missing_impl:
        logger.warning(
            "tools-schema: %d registered tool(s) have no implementation: %s", len(missing_impl), missing_impl
        )
    return {name: _tool_schema(tool) for name, tool in tools.items()}


def _run_tool_return(outcome: VisitOutcome) -> Any:
    """The synchronous door's body for a visit outcome.

    A plain result is the tool's own value (its wrapped secrets revealed for the one live caller);
    a run whose tool async-parked returns a park receipt instead of raising — the caller ask entries
    when the tool asked its caller, else the suspended sentinel's interaction ids; nothing ran → null.
    """
    if outcome.kind == "result":
        return unwrap_secrets(outcome.result)
    if outcome.kind == "asks":
        return {"asks": [entry.model_dump(mode="json") for entry in outcome.asks]}
    if outcome.kind == "parked":
        return outcome.suspended.model_dump(mode="json") if outcome.suspended is not None else None
    return None


@operation(
    summary="Run a registered tool synchronously",
    tags=["tools"],
    destructive=True,
    reload_gated=True,
    meta_executor=True,
    errors=[BadRequestError, NotFoundError, PermissionDeniedError, UpstreamError, OperationFailedError],
    request_model=RunToolRequest,
    response_model=OpaqueJson,
)
async def run_tool(tool_name: str, arguments: dict[str, object], subject: StateSubject | None = None) -> Any:
    """Execute an arbitrary registered tool with REAL side effects.

    Reaching this route is full-execution privilege — the Studio key runs any
    registered tool. Per-tool scoped keys are not supported.

    With the caller's own execution identity bound below, the dispatch ALSO takes
    the per-call execution decision the fire seam runs (an AND-gate over this
    route's own authz — it can only deny more, never less), and a connector-backed
    tool resolves its managed credential exactly as a fire would. Both are
    fire-parity, not new privilege; when the bind degrades to unbound the dispatch
    runs with no subject context.

    The tool starts through the shared :func:`~tai42_skeleton.interactions.visit.visit`, under
    the ``door="api"`` :class:`StateContext` the caller's named ``subject`` deposits: a tool
    whose async ask parks then indexes under that subject and returns a park receipt rather
    than raising. A tool that never parks returns its result.
    """
    # Resolve the name first: an unknown tool is a loud 404 (matching the schema route),
    # told apart from a tool that raises DURING execution. Only the unknown-tool error is
    # dressed up here — any other failure of the RESOLVE itself propagates untouched, so a
    # registry that cannot answer surfaces as itself rather than as a verdict about the
    # tool. In the DISPATCH phase below every raise is enveloped instead: a structured 500
    # carrying the caught error (unless it is already a typed operation error), never an
    # opaque "Internal Server Error".
    try:
        await tai42_app.tools.get_tool(tool_name)
    except UnknownToolError as exc:
        # A lookup raises for exactly the name it was asked, so no name check is needed.
        raise NotFoundError(f"unknown tool: {tool_name}") from exc

    # A synchronous door call carries no execution identity, so an async-parking tool
    # (a flow/agent whose ask parks) could never rebind its continuation and
    # 500'd instead of parking. Bind the caller's OWN key for the dispatch — the same
    # live-grants rebuild the crash-resume re-drive and the background submit use; a
    # caller whose key carries no authority binds nothing and behaves exactly as
    # before. An already-bound identity (an inline fire reaching this op) is never
    # clobbered. Function-local imports keep the operations→authz edge lazy,
    # matching the repo's guarded authz-edge idiom.
    from contextlib import nullcontext

    from tai42_contract.monitoring import RunAttribution

    from tai42_skeleton.access_control.user import request_identity
    from tai42_skeleton.states.api_context import api_state_context, caller_execution_identity
    from tai42_skeleton.tools.attribution import run_attribution

    # The caller's own principal, resolved once: it both attributes the run and (when no
    # execution identity is bound yet) is the key the dispatch's identity is rebuilt from.
    caller_key, _restricted = request_identity()
    async with caller_execution_identity(caller_key):
        try:
            # Deposit the caller's identity as this run's attribution so a runs-index row the
            # dispatch registers (a preset target) is born with a ``user_id`` rather than NULL;
            # ``run_tool``'s attribution stamp reads it. With no resolved caller the deposit is
            # skipped and the row's identity stays unset.
            attribution = (
                run_attribution(RunAttribution(user_id=caller_key)) if caller_key is not None else nullcontext()
            )
            # This envelope serves ONLY the live synchronous caller (a background submit
            # runs through ``submit_run``/``_supervise``, never this line), so a wrapped
            # secret in the result is revealed here for the one door that hands the caller
            # the real value; every recorder masks its own copy instead.
            with attribution, api_state_context(subject, caller_key):
                outcome = await tai42_app.interactions.visit(
                    target_name=tool_name,
                    cancel=[],
                    resume=[],
                    start=lambda extras: tai42_app.tools.run_tool(
                        tool_name, arguments, offload_sync=True, extras=extras
                    ),
                    extras={},
                    receives_outcome=True,
                )
                return _run_tool_return(outcome)
        except ToolResultEncodingError as exc:
            # The tool ran but produced output no JSON encoder can render (a lone UTF-16
            # surrogate). That is the tool's OUTPUT at fault, not the caller's request (never a
            # 4xx) and not an unexpected server bug (never the 500 the wire encode would otherwise
            # trigger): a 502 naming the tool and the offending JSON path. Caught before the
            # generic handler so it maps to the bad-tool-output error rather than a flat failure.
            logger.warning("run-tool: %r produced a result that cannot be JSON-encoded at %s", tool_name, exc.json_path)
            raise UpstreamError(str(exc), extra={"tool": exc.tool_name, "path": exc.json_path}) from exc
        except UnknownToolError as exc:
            # Discriminate by NAME: the requested tool vanishing between lookup and dispatch
            # (a concurrent reload) is still a 404, warned because the caller sees only a
            # plain 404; a DIFFERENT tool failing to resolve is a raise DURING execution and
            # takes the structured-500 path, never the requested tool's 404.
            if exc.tool_name == tool_name:
                logger.warning(
                    "run-tool: %r resolved at lookup but did not resolve at dispatch; answering 404", tool_name
                )
                raise NotFoundError(f"unknown tool: {tool_name}") from exc
            logger.exception("run-tool %r raised unknown-tool %r during execution", tool_name, exc.tool_name)
            raise OperationFailedError(str(exc)) from exc
        except OperationError:
            # A typed operation error is the tool's own answer (e.g. a PermissionDeniedError 403);
            # flattening it into ``OperationFailedError`` would report a refusal as a crash.
            raise
        except Exception as exc:
            logger.exception("run-tool %r raised during execution", tool_name)
            # A bare raise stringifies to ""; the class-name fallback keeps the envelope
            # from emitting {"error": ""}.
            raise OperationFailedError(str(exc) or type(exc).__name__) from exc


@operation(
    summary="Reload one app tool from its stored definition",
    tags=["tools"],
    destructive=True,
    reload_gated=True,
    request_model=ToolReloadRequest,
    response_model=FleetResult,
)
async def reload_tool(kind: str, name: str, targets: list[str] | None = None) -> Any:
    """Re-register one app tool (e.g. kind "example_tool") from its current stored definition.

    Applied on this worker and broadcast to the fleet (all workers, or only
    ``targets``); each worker re-reads the definition itself, so the op carries only
    the kind and name. The response embeds the per-worker fleet report.
    """
    return await broadcast(
        {"op": "reload_tool", "kind": kind, "name": name},
        targets,
        lambda: tai42_app.admin.run_tool_reload(kind, "reload", name),
    )


@operation(
    summary="Remove one app tool from the live registry",
    tags=["tools"],
    destructive=True,
    reload_gated=True,
    request_model=ToolReloadRequest,
    response_model=FleetResult,
)
async def remove_tool(kind: str, name: str, targets: list[str] | None = None) -> Any:
    """Remove one app tool (e.g. kind "example_tool") from the live registry.

    Applied on this worker and broadcast to the fleet (all workers, or only
    ``targets``); the response embeds the per-worker fleet report.
    """
    return await broadcast(
        {"op": "remove_tool", "kind": kind, "name": name},
        targets,
        lambda: tai42_app.admin.run_tool_reload(kind, "remove", name),
    )
