"""A chained follow-up whose predecessor SUCCEEDS but whose callback execution FAILS.

The predecessor tool runs in the backend worker and returns its result to the caller;
its chained callback runs a follow-up tool that always raises. This drives the chaining
through the platform door end-to-end and proves three properties on a real worker:

* the predecessor's own result stays a success — the follow-up's failure never corrupts
  it;
* nothing hangs — the sync call returns within a bounded deadline and the failure
  observation completes within a bounded poll, and the failed follow-up never wedges the
  worker (a later dispatch still returns);
* on the arq leg — the only backend with a queryable failed-task index — the failed
  callback surfaces on ``backend_list_failed_tasks`` carrying its real error text, while
  the successful predecessor is absent from that listing.

``backend_list_failed_tasks`` raises ``NotImplementedError`` on rq and celery (no
queryable failed-task index), so that direct read is asserted on arq alone, guarded by
the live backend kind — never a swallowed exception. On rq and celery the honest
propagation of the broken callback into each backend's own failed-job machinery is
carried by their per-backend unit suites; here the backend-agnostic properties above
hold on every leg.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

# The follow-up's deterministic error text — the marker the failed-task row must carry.
_FAILURE_MARKER = "e2e-chained-callback-boom"

# Bounds: a sync dispatch must return within this, and the failed row must appear within
# this. A breach raises loudly rather than hanging.
_SYNC_CALL_DEADLINE = 60.0
_FAILED_LISTING_DEADLINE = 30.0


def _scalar(result: Any) -> Any:
    """The scalar payload of a tool call (a tool returning a plain value)."""
    return result.data if result.data is not None else result.structured_content


def _rows(result: Any) -> list[dict[str, Any]]:
    """The list-of-rows payload of a listing tool, unwrapped from either the deserialized
    ``data`` or the ``{"result": [...]}`` structured envelope; an unexpected shape raises."""
    data = result.data
    if isinstance(data, list):
        return data
    for candidate in (result.structured_content, data):
        if isinstance(candidate, dict) and isinstance(candidate.get("result"), list):
            return candidate["result"]
    raise AssertionError(
        f"unexpected failed-task listing shape: data={data!r} structured={result.structured_content!r}"
    )


async def _dispatch(mcp: Any, payload: str, callback: dict[str, Any] | None = None) -> Any:
    """Run ``e2e_echo(payload)`` inside the backend worker via ``run_tool_sync_task`` and
    return its result, optionally chaining ``callback`` over it. Bounded so a hung
    dispatch raises rather than blocks."""
    arguments: dict[str, Any] = {"tool_name": "e2e_echo", "arguments": {"payload": payload}}
    if callback is not None:
        arguments["callback_kwargs"] = callback
    return await asyncio.wait_for(
        mcp.call_tool("run_tool_sync_task", arguments, retry_on_reloading=True),
        timeout=_SYNC_CALL_DEADLINE,
    )


async def test_failed_chained_callback_is_isolated_and_never_wedges_the_worker(core_stack: TaiStack) -> None:
    backend = core_stack.infra.variants.backend.name
    # The callback runs e2e_fail; the jq expr builds a constant {message: <marker>} over
    # the predecessor result, so the follow-up raises ValueError(<marker>).
    callback = {"tool": "e2e_fail", "expr": {"content": f'{{message: "{_FAILURE_MARKER}"}}'}}

    async with core_stack.mcp() as mcp:
        # The predecessor SUCCEEDS: the sync call returns its own result, the chained
        # follow-up's failure notwithstanding.
        predecessor = await _dispatch(mcp, "predecessor-ok", callback)
        assert _scalar(predecessor) == "predecessor-ok", predecessor

        # The failed follow-up did not wedge the worker: a later dispatch still returns.
        follow_up = await _dispatch(mcp, "worker-still-dispatching")
        assert _scalar(follow_up) == "worker-still-dispatching", follow_up

        if backend != "arq":
            pytest.skip(
                f"{backend}: backend_list_failed_tasks raises NotImplementedError — no queryable "
                "failed-task index; the honest failure propagation is proven by this backend's unit suite"
            )

        async def failed_callback_row() -> dict[str, Any] | None:
            listed = await mcp.call_tool("backend_list_failed_tasks", retry_on_reloading=True)
            for row in _rows(listed):
                if _FAILURE_MARKER in str(row.get("error", "")):
                    return row
            return None

        # The failed follow-up is a FAILED job carrying its real error text.
        row = await wait_for_async(
            failed_callback_row,
            deadline=_FAILED_LISTING_DEADLINE,
            message="the failed chained callback never surfaced on backend_list_failed_tasks",
        )
        assert _FAILURE_MARKER in row["error"], row

        # The predecessor's own success is not in the failed listing — the failure row is
        # the callback job, not the predecessor.
        final = _rows(await mcp.call_tool("backend_list_failed_tasks", retry_on_reloading=True))
        assert not any("predecessor-ok" in str(r.get("error", "")) for r in final), final
