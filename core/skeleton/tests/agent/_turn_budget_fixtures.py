"""Plain (non-agent) tools for the turn-budget tests.

Registered through ``@tai42_app.tools.tool`` and loaded ONLY through a manifest
``tools:`` entry naming this module — kept apart from ``_fixtures.py`` (which loads
under agents-only manifests, where a tool decorator would raise on a module absent
from the tools section). A turn-budget test uses these to observe the budget covering
a NON-agent tool call, a NESTED dispatch through the shared ``run_tool`` seam, and an
offloaded synchronous body the cancellation cannot interrupt. The completion / observed
flags are module globals read back off the LIVE module object (the module is re-imported
on each ``app_context`` enter).
"""

from __future__ import annotations

import asyncio
import time

from tai42_contract.app import tai42_app

from tai42_skeleton.tools.turn_budget import mark_parked_question

slow_tool_completed = False


@tai42_app.tools.tool
async def slow_tool(seconds: float = 0.0) -> str:
    """Sleep ``seconds`` then report completion — the plain-tool stand-in proving the
    turn budget covers a non-agent tool call. A run cancelled on expiry leaves the
    completion flag False."""
    global slow_tool_completed
    await asyncio.sleep(seconds)
    slow_tool_completed = True
    return "slow-done"


nested_inner_completed = False
nested_inner_saw_armed: bool | None = None


@tai42_app.tools.tool
async def nested_inner(seconds: float = 0.0) -> str:
    """A tool reached through the shared seam from ``nested_outer``. Records whether the
    turn budget was ALREADY armed at entry (the re-entrancy guard, so it opens no window
    of its own) and whether it completed; an outer window cancelling it mid-sleep leaves
    the completion flag False."""
    global nested_inner_completed, nested_inner_saw_armed
    from tai42_skeleton.tools.turn_budget import _turn_budget_armed

    nested_inner_saw_armed = _turn_budget_armed.get()
    await asyncio.sleep(seconds)
    nested_inner_completed = True
    return "inner-done"


@tai42_app.tools.tool
async def nested_outer(seconds: float = 0.0) -> str:
    """Call ``nested_inner`` through the shared ``run_tool`` seam mid-turn, so the nested
    dispatch is what the turn budget's re-entrancy guard governs."""
    return await tai42_app.tools.run_tool("nested_inner", {"seconds": seconds})


parked_question_completed = False


@tai42_app.tools.tool
async def parked_question_tool(seconds: float = 0.0) -> str:
    """Block ``seconds`` with a question parked; on the turn-budget cancellation stamp the
    pending ``(interaction_id, question)`` on the CancelledError exactly as the ``ask_user``
    answer wait does, so the expiry error names what the turn was killed waiting on. A run
    cancelled on expiry leaves the completion flag False."""
    global parked_question_completed
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError as exc:
        mark_parked_question(exc, "iid-1", "what is the status?")
        raise
    parked_question_completed = True
    return "parked-done"


# Records each thread that ran ``blocking_sync_tool`` to its end — a marker the turn
# budget cannot prevent once the body is offloaded to a worker thread.
blocking_sync_completions: list[str] = []


@tai42_app.tools.tool
def blocking_sync_tool(seconds: float = 0.0) -> str:
    """A plain (non-coroutine) tool offloaded to a worker thread via ``asyncio.to_thread``.
    Blocks ``seconds`` with ``time.sleep`` then records completion. When the turn budget
    expires the awaiting caller is answered with ``TurnTimeoutError``, but Python cannot
    cancel a running thread, so this body runs to its end and records here regardless."""
    time.sleep(seconds)
    blocking_sync_completions.append("blocking_sync_tool")
    return "blocking-done"
