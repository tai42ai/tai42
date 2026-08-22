"""The single arming point for the synchronous turn budget.

Every synchronous tool call flows through one of two live-caller doors: the shared
``ToolBinding.run_tool`` execution seam (the sync HTTP run-tool door, the agent run
tool, and every in-process re-dispatch) and the MCP session tool-call edge (which
dispatches to ``Tool.run`` through the FastMCP middleware chain, never the seam). Both
arm the budget through :func:`turn_budget`.

Re-entrancy: only the OUTERMOST tool call of a turn opens a window. A ContextVar guard
is set while a window is held; a nested call (a nested tool dispatch mid-turn) sees the
guard armed and runs unwrapped, so the outer window bounds the whole turn rather than
each nested call resetting the clock. A detached run (no live caller) and an unset
budget both run unbounded.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from fastmcp.server.middleware import Middleware
from tai42_kit.utils.detached_util import in_detached_run

from tai42_skeleton.exceptions.exceptions import TurnTimeoutError
from tai42_skeleton.settings.turn import turn_settings
from tai42_skeleton.tools.attribution import stamp_run_attribution

if TYPE_CHECKING:
    from fastmcp.server.middleware import MiddlewareContext

# Armed while the outermost tool call of a turn holds the budget window; a nested
# ``turn_budget`` seen armed runs unwrapped so it cannot open a fresh window.
_turn_budget_armed: ContextVar[bool] = ContextVar("tai42_turn_budget_armed", default=False)

_PARKED_QUESTION_ATTR = "tai42_parked_question"


def mark_parked_question(exc: BaseException, interaction_id: str, question: str, sensitive: bool) -> None:
    """Stamp the pending question on a cancellation unwinding out of a parked answer wait.

    The turn-budget expiry cancels the awaited turn; when that cancellation is
    unwinding out of an ``ask_user`` answer wait it carries the pending question here
    so the ``TurnTimeoutError`` can name what the turn was killed waiting on. A
    ``sensitive`` question is stamped too, but its text is redacted in the expiry
    message so a credential prompt never reaches the error string.
    """
    setattr(exc, _PARKED_QUESTION_ATTR, (interaction_id, question, sensitive))


@asynccontextmanager
async def turn_budget() -> AsyncIterator[None]:
    """Arm the turn budget around the wrapped tool execution, for the OUTERMOST call.

    Reads ``TAI_TURN_TIMEOUT_SECONDS`` at call time. Runs the body unwrapped when the
    budget is unset, the run is detached (no live caller), or an outer call already
    armed the window. Otherwise expiry cancels the awaiting turn and raises a loud,
    typed ``TurnTimeoutError``; a builtin ``TimeoutError`` the turn's own work raised
    (which the cancellation did not cause) passes through unchanged. The cancel reaches
    only the awaited coroutine: a synchronous tool body offloaded to a worker thread
    cannot be interrupted and may run to completion in the background after the caller
    has been answered.
    """
    timeout = turn_settings().timeout_seconds
    if timeout is None or in_detached_run() or _turn_budget_armed.get():
        yield
        return
    token = _turn_budget_armed.set(True)
    try:
        cm = asyncio.timeout(timeout)
        try:
            async with cm:
                yield
        except TimeoutError as exc:
            if cm.expired():
                parked = getattr(exc.__cause__, _PARKED_QUESTION_ATTR, None)
                if parked is not None:
                    interaction_id, question, sensitive = parked
                    if sensitive:
                        raise TurnTimeoutError(
                            f"turn exceeded the {timeout:g}s turn timeout while waiting on an "
                            f"unanswered question (interaction {interaction_id}): [sensitive question]"
                        ) from None
                    raise TurnTimeoutError(
                        f"turn exceeded the {timeout:g}s turn timeout while waiting on an "
                        f"unanswered question (interaction {interaction_id}): {question}"
                    ) from None
                raise TurnTimeoutError(f"turn exceeded the {timeout:g}s turn timeout") from None
            raise
    finally:
        _turn_budget_armed.reset(token)


class TurnBudgetMiddleware(Middleware):
    """Arm the synchronous turn budget at the MCP session tool-call edge.

    An MCP ``tools/call`` dispatches through the FastMCP middleware chain to
    ``Tool.run`` directly, never the ``ToolBinding.run_tool`` seam, so this door arms
    the budget itself. Added INNERMOST (after the authz/reload middleware) so a denied
    or reload-rejected call never opens a window; the shared ContextVar guard keeps a
    nested in-process re-dispatch (a tool body reaching ``run_tool``) from opening a
    second one."""

    async def on_call_tool(
        self,
        context: "MiddlewareContext[Any]",
        call_next: Callable[["MiddlewareContext[Any]"], Awaitable[Any]],
    ) -> Any:
        with stamp_run_attribution():
            async with turn_budget():
                return await call_next(context)


async def drive_live_caller_astream[EventT](stream: AsyncIterator[EventT]) -> AsyncIterator[EventT]:
    """Drive a live-caller agent ``astream`` under the turn budget AND the attribution
    scope — the ONE shared seam every live-caller agent-run door routes through.

    The three live-caller doors that call ``agent.astream`` directly (the conversation
    bridge and both agent-run SSE routes) hold a live client connection, so they are NOT
    in the design-exempt detached set and MUST be budgeted; routing them here keeps the
    budget single-sourced and stamps their run's trace with its attribution, so no future
    door can add an unbudgeted/unattributed astream. On expiry :func:`turn_budget` raises
    ``TurnTimeoutError`` out through this iterator to the door's existing raise path;
    ``turn_budget``'s re-entrancy guard keeps a nested tool re-dispatch from opening a
    second window. Detached runs stay exempt by ``turn_budget``'s own ``in_detached_run``
    no-op — this seam never arms a budget there."""
    with stamp_run_attribution():
        async with turn_budget():
            async for event in stream:
                yield event
