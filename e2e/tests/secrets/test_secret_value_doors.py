"""The generic secret-value envelope across the tool-execution doors.

The ``e2e_secret_value`` fixture tool returns a real credential wrapped in a
``SecretValue``. The live sync run-tool door and a genuine MCP call REVEAL the
real value to the caller; a background-submitted run RECORDS the masked
``[secret]`` placeholder, and the real value never appears anywhere in the
stored record. Driven on the auth-off core stack, which mounts both the
``/api/run-tool`` and ``/api/tool-runs`` doors and serves the MCP surface."""

from __future__ import annotations

import json
from collections.abc import Callable

from tai42_contract.secrets import SECRET_PLACEHOLDER

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

_TOOL = "e2e_secret_value"


async def test_sync_run_tool_door_reveals_the_real_value(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    secret = uniq("credential")
    result = await core_stack.api().post(
        "/api/run-tool", json={"tool_name": _TOOL, "arguments": {"credential": secret}}
    )
    assert result == {"credential": secret}, result


async def test_background_run_records_the_masked_placeholder(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    secret = uniq("credential")
    api = core_stack.api()
    submitted = await api.post(
        "/api/tool-runs", json={"tool_name": _TOOL, "arguments": {"credential": secret}}, expect=202
    )
    run_id = submitted["run_id"]

    async def terminal() -> dict | None:
        record = await api.get(f"/api/tool-runs/{run_id}")
        return record if record["status"] == "succeeded" else None

    record = await wait_for_async(terminal, deadline=10.0, message="background secret run never reached succeeded")
    assert record["result"] == {"credential": SECRET_PLACEHOLDER}, record
    # The real value never entered the stored record, on any field.
    assert secret not in json.dumps(record), record


async def test_mcp_call_reveals_the_real_value(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    secret = uniq("credential")
    async with core_stack.mcp(port=core_stack.port_a) as mcp:
        result = (await mcp.call_tool(_TOOL, {"credential": secret}, retry_on_reloading=True)).data
    assert result == {"credential": secret}, result
