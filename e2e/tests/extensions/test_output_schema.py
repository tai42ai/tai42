"""The ``output_schema`` tool extension (TRANSFORMER, config-accepting)
through the stack.

The only toolbox extension that takes AUTHOR-BOUND config: the manifest binds a
JSON Schema under the combo's ``config`` (``{"name": "output_schema", "config":
{"schema": ...}}``). Distinct from the preset ``output_schema`` field, which is the
skeleton's own preset-bind path — this is the manifest-bound branch.

Observable effects asserted: (1) the branch ADVERTISES the configured schema as its
output schema; (2) a result matching the schema passes through unchanged; (3) a
result violating it raises LOUDLY at the call rather than passing silently."""

from __future__ import annotations

import json

import pytest

from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless


async def test_branch_advertises_the_configured_output_schema(extensions_stack: TaiStack) -> None:
    async with extensions_stack.mcp() as mcp:
        tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "e2e_worker_info_output_schema" in tools, f"output_schema branch not served: {sorted(tools)}"
    advertised = tools["e2e_worker_info_output_schema"].outputSchema
    assert advertised is not None, "output_schema branch advertised no output schema"
    # The configured schema's own property reached the advertised schema.
    assert "pid" in advertised.get("properties", {}), f"configured schema not advertised: {advertised}"


async def test_conforming_result_passes_and_violating_result_raises(extensions_stack: TaiStack) -> None:
    async with extensions_stack.mcp() as mcp:
        # e2e_worker_info returns a dict carrying an integer ``pid`` — it satisfies
        # the configured schema, so the branch validates it and passes it through.
        # The client hydrates the result against the ADVERTISED schema, so ``data``
        # comes back as the schema's synthesized model rather than a raw dict.
        ok = await mcp.call_tool("e2e_worker_info_output_schema", {})
        assert not ok.is_error
        assert isinstance(ok.structured_content, dict)
        assert isinstance(ok.structured_content["pid"], int)

        # e2e_echo returns a STRING, which cannot satisfy the object schema its
        # branch is configured with: the extension validates and raises loudly
        # rather than letting a non-conforming result through.
        served = {tool.name for tool in await mcp.list_tools()}
        assert "e2e_echo_output_schema" in served, f"the violating branch is not even served: {sorted(served)}"
        bad = await mcp.call_tool("e2e_echo_output_schema", {"payload": "not-an-object"}, raise_on_error=False)
    assert bad.is_error, "output_schema let a result that violates the configured schema pass silently"
    # The failure is the SCHEMA rejecting the result — not a missing tool, a transport
    # error, or any other incidental failure that ``is_error`` alone would accept.
    message = json.dumps([block.model_dump(mode="json") for block in (bad.content or [])])
    assert "does not match schema" in message, f"the error does not name the schema violation: {message}"
    assert "is not of type 'object'" in message, f"the error does not name the violated constraint: {message}"
