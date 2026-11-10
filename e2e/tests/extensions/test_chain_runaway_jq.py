"""A runaway jq program in the ``chain`` extension, through the stack.

Observable effect: a chain whose jq expression never ends (``last(range(1e18))``:
constant memory, no output, no end) fails with the jq budget's error once
``JQ_TIMEOUT_SECONDS`` elapses, and the second tool is never called. While that
call is in flight the same single serve worker answers every ``/health`` poll
fast: the program runs in a compute worker process, not on the server's event
loop, so the loop keeps serving and the supervisor's health check keeps passing.
A follow-up ordinary chain call on the same stack drives the second tool, the
proof the killed compute worker was replaced and the server is unharmed."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = [pytest.mark.backendless, pytest.mark.needs("probe-tools", "store:redis", "setting:extension:chain")]

# A jq program that never ends and holds constant memory.
_NEVER_ENDS = "last(range(1e18))"
# The bound of one ``/health`` poll; a poll past it raises out of the wait.
_HEALTH_TIMEOUT = 1.0
# The bound of the whole runaway call: the stack's 2 s jq budget plus room for the
# worker kill and the error's way back over MCP.
_RUNAWAY_CALL_DEADLINE = 30.0


async def test_runaway_jq_program_is_killed_while_the_server_keeps_serving(
    extensions_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    key = uniq("chain-runaway")
    api = extensions_stack.api()
    latencies: list[float] = []

    async with extensions_stack.mcp() as mcp:
        call = asyncio.create_task(
            mcp.call_tool(
                "e2e_echo_chain",
                {
                    "payload": f"runaway-{uniq('body')}",
                    "jq_expression": {"content": _NEVER_ENDS},
                    "next_tool_name": "e2e_record",
                },
                raise_on_error=False,
            )
        )

        async def health_polled_until_the_call_ends() -> bool:
            if call.done():
                return True
            started = time.monotonic()
            response = await api.request_raw("GET", "/health", timeout=_HEALTH_TIMEOUT, retry_on_reloading=False)
            latencies.append(time.monotonic() - started)
            assert response.status_code == 200, f"/health answered {response.status_code} during the jq call"
            return False

        try:
            await wait_for_async(
                health_polled_until_the_call_ends,
                deadline=_RUNAWAY_CALL_DEADLINE,
                message="the chain call with a runaway jq program never ended",
            )
        finally:
            if not call.done():
                call.cancel()
        result = await call

        # The polls ran while the call was in flight, and every one answered well inside its bound.
        assert len(latencies) >= 3, f"too few /health polls landed during the jq call: {latencies}"
        assert max(latencies) < _HEALTH_TIMEOUT, f"/health stalled during the jq call: {latencies}"

        # The call failed on the jq budget — not a transport error or any other failure.
        assert result.is_error, "a jq program that never ends returned a result"
        message = json.dumps([block.model_dump(mode="json") for block in (result.content or [])])
        assert "jq evaluation exceeded" in message, f"the error does not name the jq budget: {message}"
        assert "JQ_TIMEOUT_SECONDS" in message, f"the error does not name the jq budget setting: {message}"
        assert extensions_stack.records(key) == [], "the chain drove the second tool after a failed transform"

        # The same stack serves an ordinary chain call: the killed compute worker was replaced.
        payload = f"after-runaway-{uniq('body')}"
        await mcp.call_tool(
            "e2e_echo_chain",
            {
                "payload": payload,
                "jq_expression": {"content": f'{{key: "{key}", value: .}}'},
                "next_tool_name": "e2e_record",
            },
        )

    async def recorded() -> bool:
        return len(extensions_stack.records(key)) >= 1

    await wait_for_async(recorded, deadline=5.0, message="the chain after the runaway call never drove the second tool")
    records = extensions_stack.records(key)
    assert len(records) == 1, f"chain drove the second tool the wrong number of times: {records}"
    assert payload in records[0], f"chain did not transform echo's output into the next tool's input: {records[0]}"
