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
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import Middleware
from tai42_contract.interactions import SuspendedInteraction, read_suspended_interaction_marker
from tai42_contract.monitoring import RunAttribution
from tai42_contract.tools import (
    ToolInvocation,
    current_tool_invocation,
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
from tai42_skeleton.tools.state_binding import (
    apply_binding_injections,
    apply_binding_updates,
    merge_bindings,
)
from tai42_skeleton.tools.turn_budget import turn_budget

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastmcp.server.middleware import MiddlewareContext

    from tai42_skeleton.app.server import TaiMCP


class DispatchScope:
    """The handle a dispatch entrance drives to record the run's terminal outcome.

    Invariant both dispatch doors share: binding UPDATES apply ONLY on a real output. A
    park — an async-suspend ``SuspendedInteraction`` returned up through the in-process
    door, or the equivalent wire marker at the MCP edge — is NOT a clean output, so it
    applies no updates (its injections, which ran before the dispatch, still stand).
    :meth:`observe` takes the raw in-process result and treats a ``SuspendedInteraction``
    as a park by TYPE; :meth:`observe_park` records a park recognized from the MCP wire
    marker by its interaction id. Both record the park on the runs-index row and no-op
    when no row was opened — a non-preset/draft dispatch, a nested sub-preset dispatch, or
    the store OFF."""

    def __init__(self) -> None:
        self._run: RunRecord | None = None
        # The dispatch's terminal result and whether it was a clean (non-park) success —
        # read by the binding apply-after so state updates run ONLY on a real output. A
        # raised dispatch never reaches ``observe`` (its exception unwinds the scope), so an
        # errored run applies no updates.
        self._result: object = None
        self._succeeded: bool = False

    def observe(self, result: object) -> None:
        self._result = result
        # A park (a ``SuspendedInteraction`` return) is not a clean output — no updates apply.
        # Mirrors ``RunRecord.observe``, so the in-process door and the MCP edge (via
        # ``observe_park``) share one invariant: updates run ONLY on a real output.
        self._succeeded = not isinstance(result, SuspendedInteraction)
        if self._run is not None:
            self._run.observe(result)

    def observe_park(self, interaction_id: str) -> None:
        # A park is not a clean output — no updates apply to a suspended run.
        self._succeeded = False
        if self._run is not None:
            self._run.observe_park(interaction_id)


# Armed for the span of a dispatch so a NESTED dispatch (a tool that itself dispatches a
# tool) never re-applies the binding — it is applied ONCE, at the outermost dispatch (the
# door target). Independent of the preset attribution guard, which the runs-index/trace root
# uses and which arms only for registered presets.
_binding_scope_armed: ContextVar[bool] = ContextVar("tai42_binding_scope_armed", default=False)


@asynccontextmanager
async def _binding_application(
    app: TaiMCP, merged: object, arguments: dict[str, Any] | None, scope: DispatchScope, door_id: str
) -> AsyncIterator[None]:
    """Apply the merged binding ONCE around the dispatch: injections write ``arguments`` (in
    place) BEFORE it, updates apply through the store AFTER a clean success. A ``None`` merge
    — no binding, or a nested dispatch that is not the outermost — is a guarded no-op, so a
    pure tool is byte-for-byte untouched.

    Injections and updates share ONE guard: whenever the merge is a binding they both engage,
    over ``arguments`` itself (an empty dict kept by identity, so an in-place injection reaches
    the dispatch) or a fresh ``{}`` when the door carried none. A dispatch with no arguments
    never silently skips its injections."""
    from tai42_contract.states import StateBinding

    args = arguments if arguments is not None else {}
    if isinstance(merged, StateBinding):
        await apply_binding_injections(app, merged, args)
    yield
    if isinstance(merged, StateBinding) and scope._succeeded:
        await apply_binding_updates(app, merged, args, scope._result, door_id)


@asynccontextmanager
async def dispatch_scope(
    app: TaiMCP, key: str, arguments: dict[str, Any] | None = None
) -> AsyncIterator[DispatchScope]:
    """Arm the shared run lifecycle around a dispatch of ``key``, yielding the
    :class:`DispatchScope` the caller drives with the dispatch outcome.

    Deposits the ambient invoked-tool seam (CARRYING FORWARD any door binding a prior
    deposit left on it, read-then-set), enters the run-attribution stamp and the turn
    budget, and — when ``key`` is a REGISTERED preset with a retained active version —
    layers the preset version stamp and, for the OUTERMOST such dispatch, the runs-index
    record (which opens the trace root). A nested sub-preset dispatch sees the armed guard
    and adds no second stamp or row; a non-preset/draft key (version ``None``) adds neither.
    Retry and bound-identity authorization live in the caller's dispatch, not here.

    The door/preset binding applies ONCE, around the OUTERMOST dispatch (the door target),
    INDEPENDENT of whether that target is a preset: a preset target merges the carried door
    binding with the preset's OWN binding (door precedence, :func:`merge_bindings`); a plain
    tool target applies the carried door binding alone; a bare preset run applies the preset's
    own binding alone. A NESTED dispatch (the armed guard) applies nothing — a sub-preset's
    own binding never fires, and the door binding is never re-applied. Injections write
    ``arguments`` (in place) before the dispatch, updates apply after a clean success."""
    scope = DispatchScope()
    prior = current_tool_invocation()
    door_binding = prior.state_binding if prior is not None else None
    outermost_binding = not _binding_scope_armed.get()
    invocation_token = set_current_tool_invocation(ToolInvocation(tool_name=key, state_binding=door_binding))
    binding_token = _binding_scope_armed.set(True)
    try:
        with stamp_run_attribution():
            async with turn_budget():
                manager = app.preset_manager
                is_preset = manager.is_registered(key)
                version = manager.active_version(key) if is_preset else None
                # The preset's own binding merges only when THIS dispatch is the door target
                # (outermost) AND a registered preset; the door binding is carried in ``prior``.
                preset_binding = manager.get_spec(key).state_binding if (is_preset and outermost_binding) else None
                merged = merge_bindings(door_binding, preset_binding) if outermost_binding else None
                async with _binding_application(app, merged, arguments, scope, key):
                    if version is None:
                        yield scope
                        return
                    # Read the outermost state BEFORE the stamp arms it: True here means an
                    # ancestor preset dispatch already owns this run's row.
                    outermost_preset = not preset_attribution_armed()
                    with stamp_preset_attribution(key, version):
                        if not outermost_preset:
                            yield scope
                            return
                        async with record_outermost_preset_run(key, version) as run:
                            scope._run = run
                            yield scope
    finally:
        _binding_scope_armed.reset(binding_token)
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
        # The MCP call's arguments, mutated in place by any binding injection so the edge
        # dispatches the injected input. Absent arguments (the wire's ``None``) are bound to
        # ONE real dict set back on the message, so the injection target and the dispatched
        # arguments are the same object — a throwaway ``{}`` would drop the injection.
        mcp_arguments = getattr(context.message, "arguments", None)
        if isinstance(mcp_arguments, dict):
            binding_args = mcp_arguments
        else:
            binding_args = {}
            context.message.arguments = binding_args
        user_id, _restricted = request_identity()
        attribution = run_attribution(RunAttribution(user_id=user_id)) if user_id is not None else nullcontext()
        with attribution:
            async with dispatch_scope(self._app, name, binding_args) as scope:
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
