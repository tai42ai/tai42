"""A probe that runs a tool and answers with its result only after a hold.

Run as a backend job, the hold keeps the job running after the tool it ran has returned, so a
test can observe what the fleet does between a change made inside a job and that job's answer.
"""

from __future__ import annotations

import asyncio

from tai42_contract.app import tai42_app

# The longest hold a call may ask for, so a mis-scripted test cannot hold a job for long.
_MAX_HOLD_SECONDS = 60.0


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_run_then_hold(tool_name: str, arguments: dict[str, object], seconds: float) -> object:
    """Run the tool ``tool_name`` with ``arguments``, then hold ``seconds`` (at most 60) and answer its result."""
    if seconds > _MAX_HOLD_SECONDS:
        raise ValueError(f"e2e_run_then_hold bound exceeded: {seconds} > {_MAX_HOLD_SECONDS} seconds")
    result = await tai42_app.tools.run_tool(tool_name, arguments)
    await asyncio.sleep(seconds)  # noqa: TID251 — the observed hold, bounded above
    return result
