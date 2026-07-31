"""C3 / M2 — cross-entrypoint lifecycle on the shared mmap dir. A metrics-server
(reader) restart must lose nothing; a full run-family restart wipes exactly once
before workers spawn."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.manifests import PROBE_TOOLS_TITLE
from tai42_e2e.stack import TaiStack

_FAMILY = "tai_tool_call_count_total"
_LABELS = {"name": "e2e_echo", "runtime": "main", "title": PROBE_TOOLS_TITLE}


async def _drive(stack: TaiStack, n: int) -> None:
    async with stack.mcp() as mcp:
        for _ in range(n):
            await mcp.call_tool("e2e_echo_prometheus_metrics", {"payload": "x"})


async def _await_sample(stack: TaiStack, expected: float) -> None:
    async def scraped() -> bool:
        value = stack.scrape().sample(_FAMILY, _LABELS)
        return value is not None and value >= expected

    await wait_for_async(scraped, deadline=5.0, message=f"scrape never reached {expected}")


async def test_metrics_server_restart_loses_nothing(fresh_stack: Callable[..., TaiStack]) -> None:
    stack = fresh_stack()
    await _drive(stack, 3)
    await _await_sample(stack, 3)
    stack.restart("metrics")
    await _drive(stack, 2)
    # A reader restart must never wipe the writers' mmap files.
    await _await_sample(stack, 5)


async def test_full_restart_resets_cleanly(fresh_stack: Callable[..., TaiStack]) -> None:
    stack = fresh_stack()
    await _drive(stack, 3)
    await _await_sample(stack, 3)
    # Restart the whole run family (serve stamps a fresh run id and is the wipe
    # owner of the dir). One wipe before workers spawn — no stale accumulation.
    stack.restart("serve")
    stack.restart("backend")
    stack.restart("metrics")
    await _drive(stack, 2)

    async def reads_exactly_two() -> bool:
        value = stack.scrape().sample(_FAMILY, _LABELS)
        return value is not None and value == 2

    await wait_for_async(
        reads_exactly_two, deadline=5.0, message="counter did not reset to exactly 2 after a full restart"
    )
