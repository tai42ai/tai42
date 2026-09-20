"""The ``batch`` tool extension (composed-signature) through the stack.

Observable effect: batching a tool runs it once per entry in ``params`` and returns
the results IN INPUT ORDER. A batch that memoized a constant, ran one entry, or
scrambled order would be caught here — two distinct payloads must come back as their
own two results, in the order sent."""

from __future__ import annotations

import json

import pytest

from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless


async def test_batch_runs_each_entry_in_input_order(extensions_stack: TaiStack) -> None:
    async with extensions_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "e2e_echo_batch",
            {"params": [{"payload": "first"}, {"payload": "second"}, {"payload": "third"}]},
        )

    # The batch returns one result per input entry, in order — a list of the three
    # echoed payloads. Serialize once so the shape (list vs scalar) is asserted too.
    payload = json.dumps(result.data)
    assert result.data == ["first", "second", "third"], f"batch did not run each entry in order: {payload}"
