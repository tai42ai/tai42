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

if TYPE_CHECKING:
    from fastmcp.server.middleware import MiddlewareContext

# Armed while the outermost tool call of a turn holds the budget window; a nested
# ``turn_budget`` seen armed runs unwrapped so it cannot open a fresh window.
_turn_budget_armed: ContextVar[bool] = ContextVar("tai42_turn_budget_armed", default=False)


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
        except TimeoutError:
            if cm.expired():
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
        async with turn_budget():
            return await call_next(context)
