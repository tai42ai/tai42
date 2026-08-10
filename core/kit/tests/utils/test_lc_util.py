"""lc_util: MCP tool -> LangChain StructuredTool conversion."""

from typing import Any, cast

import mcp
import pytest

pytest.importorskip("langchain_core")

from fastmcp.client import Client
from langchain_core.tools import StructuredTool

from tai42_kit.utils.lc.lc_util import mcp_tool_to_lc_tool, mcp_tools_to_lc_tools


def _mcp_tool(name: str, description: str = "desc") -> mcp.types.Tool:
    return mcp.types.Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {"x": {"type": "string"}}},
    )


class _FakeClient:
    def __init__(self, tools: list[mcp.types.Tool] | None = None) -> None:
        self._tools = tools or []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.timeouts: list[Any] = []

    async def list_tools(self) -> list[mcp.types.Tool]:
        return self._tools

    async def call_tool(self, name: str, kwargs: dict[str, Any], timeout: Any = None) -> str:
        self.calls.append((name, kwargs))
        self.timeouts.append(timeout)
        return f"result-{name}"


def _as_client(fake: _FakeClient) -> Client[Any]:
    # The real fastmcp Client needs a live transport/server connection; the
    # conversion helpers only touch list_tools/call_tool, which this fake
    # supplies with matching signatures.
    return cast(Client[Any], fake)


def test_tool_conversion_builds_structured_tool():
    tool = mcp_tool_to_lc_tool(_as_client(_FakeClient()), _mcp_tool("search"))
    assert isinstance(tool, StructuredTool)
    assert tool.name == "search"
    assert tool.description == "desc"


async def test_tool_coroutine_calls_client_call_tool():
    client = _FakeClient()
    tool = mcp_tool_to_lc_tool(_as_client(client), _mcp_tool("search"))
    assert tool.coroutine is not None
    out = await tool.coroutine(x="q")
    assert out == "result-search"
    assert client.calls == [("search", {"x": "q"})]


async def test_tool_call_passes_configured_call_timeout():
    # The built tool bounds each downstream call with the MCP client's configured
    # call timeout rather than fastmcp's default of None (wait forever).
    from tai42_kit.clients.settings import mcp_client_settings

    client = _FakeClient()
    tool = mcp_tool_to_lc_tool(_as_client(client), _mcp_tool("search"))
    assert tool.coroutine is not None
    await tool.coroutine(x="q")
    assert client.timeouts == [mcp_client_settings().call_timeout_seconds]


def test_overlong_name_raises():
    with pytest.raises(ValueError, match="64-character limit"):
        mcp_tool_to_lc_tool(_as_client(_FakeClient()), _mcp_tool("n" * 65))


async def test_tools_to_lc_tools_returns_all_when_no_filter():
    client = _FakeClient([_mcp_tool("a"), _mcp_tool("b")])
    tools = await mcp_tools_to_lc_tools(_as_client(client))
    assert {t.name for t in tools} == {"a", "b"}


async def test_tools_to_lc_tools_applies_name_filter():
    client = _FakeClient([_mcp_tool("a"), _mcp_tool("b"), _mcp_tool("c")])
    tools = await mcp_tools_to_lc_tools(_as_client(client), ["a", "c"])
    assert {t.name for t in tools} == {"a", "c"}


async def test_tools_to_lc_tools_missing_requested_name_raises():
    # A requested tool that the server does not expose fails loudly, naming the
    # missing tool rather than silently returning fewer tools than asked for.
    client = _FakeClient([_mcp_tool("a"), _mcp_tool("b")])
    with pytest.raises(ValueError, match=r"not found on the server.*missing"):
        await mcp_tools_to_lc_tools(_as_client(client), ["a", "missing"])
