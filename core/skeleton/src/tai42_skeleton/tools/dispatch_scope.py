"""The shared tool-dispatch lifecycle both dispatch entrances enter.

Two doors dispatch a tool: the in-process ``ToolBinding.run_tool`` seam and the MCP
``tools/call`` edge (which reaches ``Tool.run`` through the FastMCP middleware chain,
never ``run_tool``). Both must arm the SAME run lifecycle around the dispatch — the
ambient invoked-tool deposit, the run-attribution stamp, the synchronous turn budget,
and, for the OUTERMOST dispatch of a registered preset, the preset version stamp plus
the runs-index record (which opens the run's trace root at the chokepoint). Homing that
lifecycle in one context manager keeps the two doors from carrying per-door copies that
drift.

The scope knows ONLY the tool name and, for a registered preset, its active version; it
never inspects what a preset wraps — the platform stays agnostic to preset contents.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import Middleware
from tai42_contract.interactions import read_suspended_interaction_marker
from tai42_contract.monitoring import RunAttribution
from tai42_contract.tools import (
    ToolInvocation,
    reset_current_tool_invocation,
    set_current_tool_invocation,
)

from tai42_skeleton.runs.chokepoint import RunRecord, record_outermost_preset_run
from tai42_skeleton.tools.attribution import (
    preset_attribution_armed,
    run_attribution,
    stamp_preset_attribution,
    stamp_run_attribution,
)
from tai42_skeleton.tools.retry import dispatch_with_retry
from tai42_skeleton.tools.turn_budget import turn_budget

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastmcp.server.middleware import MiddlewareContext

    from tai42_skeleton.app.server import TaiMCP


class DispatchScope:
    """The handle a dispatch entrance drives to record the run's terminal outcome.

    :meth:`observe` forwards the raw in-process dispatch result (a
    ``SuspendedInteraction`` sentinel records a park); :meth:`observe_park` records a
    park recognized from the MCP wire marker by its interaction id. Both no-op when no
    runs-index row was opened — a non-preset/draft dispatch, a nested sub-preset
    dispatch, or the store OFF."""

    def __init__(self) -> None:
        self._run: RunRecord | None = None

    def observe(self, result: object) -> None:
        if self._run is not None:
            self._run.observe(result)

    def observe_park(self, interaction_id: str) -> None:
        if self._run is not None:
            self._run.observe_park(interaction_id)


@asynccontextmanager
async def dispatch_scope(app: TaiMCP, key: str) -> AsyncIterator[DispatchScope]:
    """Arm the shared run lifecycle around a dispatch of ``key``, yielding the
    :class:`DispatchScope` the caller drives with the dispatch outcome.

    Deposits the ambient invoked-tool seam, enters the run-attribution stamp and the
    turn budget, and — when ``key`` is a REGISTERED preset with a retained active
    version — layers the preset version stamp and, for the OUTERMOST such dispatch, the
    runs-index record (which opens the trace root). A nested sub-preset dispatch sees
    the armed guard and adds no second stamp or row; a non-preset/draft key (version
    ``None``) adds neither. Retry and bound-identity authorization live in the caller's
    dispatch, not here."""
    scope = DispatchScope()
    invocation_token = set_current_tool_invocation(ToolInvocation(tool_name=key))
    try:
        with stamp_run_attribution():
            async with turn_budget():
                manager = app.preset_manager
                version = manager.active_version(key) if manager.is_registered(key) else None
                if version is None:
                    yield scope
                    return
                # Read the outermost state BEFORE the stamp arms it: True here means an
                # ancestor preset dispatch already owns this run's row.
                outermost = not preset_attribution_armed()
                with stamp_preset_attribution(key, version):
                    if not outermost:
                        yield scope
                        return
                    async with record_outermost_preset_run(key, version) as run:
                        scope._run = run
                        yield scope
    finally:
        reset_current_tool_invocation(invocation_token)


class DispatchScopeMiddleware(Middleware):
    """Enter the shared :func:`dispatch_scope` at the MCP ``tools/call`` edge.

    An MCP ``tools/call`` reaches ``Tool.run`` through the FastMCP middleware chain,
    never the ``ToolBinding.run_tool`` seam, so this edge arms the run lifecycle itself
    — the same scope the in-process seam enters, so a registered preset invoked over
    MCP registers a runs-index row and trace root exactly as an in-process dispatch
    does. Registered INNERMOST (after the authz/reload middleware) so a denied or
    reload-rejected call arms no scope; the scope's own re-entrancy guards keep a nested
    in-process re-dispatch from opening a second window, stamp, or row.

    The caller identity the authz middleware bound is deposited as the run attribution
    before the scope, so a row born here carries the caller's ``user_id`` rather than
    NULL. Retry rides via :func:`dispatch_with_retry` around ``call_next`` so a
    policy-armed preset re-fires here exactly as through the in-process seam."""

    def __init__(self, app: TaiMCP) -> None:
        self._app = app

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: Callable[[MiddlewareContext[Any]], Awaitable[Any]],
    ) -> Any:
        from tai42_skeleton.access_control.user import request_identity

        name = context.message.name
        user_id, _restricted = request_identity()
        attribution = run_attribution(RunAttribution(user_id=user_id)) if user_id is not None else nullcontext()
        with attribution:
            async with dispatch_scope(self._app, name) as scope:
                policy = await self._app._tool_binding.resolve_retry_policy(name)
                result = await dispatch_with_retry(name, policy, lambda: call_next(context))
                # PARK recognition (explicit — never defaulted): a parked call returns
                # the reserved suspended-interaction marker in its structured content;
                # record ``park`` with its interaction id. A well-formed non-park result
                # records ``success``. A park that instead surfaced as a raised error is
                # recorded ``error`` by the chokepoint's exception path — a park is never
                # silently recorded as success.
                marker = read_suspended_interaction_marker(getattr(result, "structured_content", None))
                if marker is not None:
                    scope.observe_park(marker["interaction_id"])
                else:
                    scope.observe(result)
                return result
