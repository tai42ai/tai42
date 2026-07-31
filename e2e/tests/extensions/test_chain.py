"""The ``chain`` tool extension (TRANSFORMER) through the stack.

Observable effect: the branch calls the wrapped tool, transforms its output with a
jq expression, then calls a second named tool with the transformed result. Branch
``e2e_echo`` with ``chain``; the jq expression turns echo's returned string into
``{key, value}`` arguments for the ``e2e_record`` probe, so the value the second
tool records on the probe channel IS the transformed output — the proof the
transform ran end to end."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless


async def test_chain_transforms_output_into_next_tool_input(
    extensions_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    key = uniq("chain")
    payload = f"chained-{uniq('body')}"

    async with extensions_stack.mcp() as mcp:
        await mcp.call_tool(
            "e2e_echo_chain",
            {
                "payload": payload,
                # Transform echo's output string into e2e_record's (key, value)
                # arguments: the key is this run's unique key, the value is the
                # echo output (``.``).
                "jq_expression": f'{{key: "{key}", value: .}}',
                "next_tool_name": "e2e_record",
            },
        )

    async def recorded() -> bool:
        return len(extensions_stack.records(key)) >= 1

    await wait_for_async(recorded, deadline=5.0, message="chain never drove the second tool")
    records = extensions_stack.records(key)
    assert len(records) == 1, f"chain drove the second tool the wrong number of times: {records}"
    # The value the second tool recorded is the wrapped tool's transformed output.
    assert payload in records[0], f"chain did not transform echo's output into the next tool's input: {records[0]}"
