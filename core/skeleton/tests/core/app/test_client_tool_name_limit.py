"""``ToolBinding.get_client_tools`` refuses a selected tool name longer than a bound tool name may be.

The handle carries the full registered name: a longer one is refused, never truncated (a
truncated name would name a different tool).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from tai42_contract.agent import TOOL_NAME_MAX_LEN
from tai42_kit.llm import ToolNameTooLongError

from tai42_skeleton.app import server as server_module
from tai42_skeleton.tools import binding as binding_module
from tai42_skeleton.tools.binding import ToolBinding, UnknownToolError

_AT_BOUND = "x" * TOOL_NAME_MAX_LEN
_PAST_BOUND = "x" * (TOOL_NAME_MAX_LEN + 1)


def _fake_tool(name):
    def fn():
        """A tool."""
        return name

    fn.__name__ = "fn"
    fake = MagicMock(spec=binding_module.FunctionTool)
    fake.fn = fn
    fake.name = name
    fake.description = "A tool."
    fake.parameters = {"type": "object", "properties": {}}
    return fake


def _binding(tool_names):
    binding = ToolBinding(MagicMock(spec=server_module.TaiMCP))
    # Instance attribute shadows the method — the binding under test reads the
    # faked tool set through its own ``get_tools``.
    binding.get_tools = AsyncMock(return_value={n: _fake_tool(n) for n in tool_names})
    return binding


def test_a_name_at_the_bound_is_bound_whole():
    handles = asyncio.run(_binding(["short_tool", _AT_BOUND]).get_client_tools())
    assert sorted(handle.name for handle in handles) == sorted(["short_tool", _AT_BOUND])


def test_a_name_past_the_bound_raises_naming_the_tool():
    with pytest.raises(ToolNameTooLongError, match=_PAST_BOUND):
        asyncio.run(_binding([_PAST_BOUND]).get_client_tools())


def test_only_the_requested_names_are_checked():
    handles = asyncio.run(_binding([_PAST_BOUND, "short_tool"]).get_client_tools(["short_tool"]))
    assert [handle.name for handle in handles] == ["short_tool"]


def test_an_unknown_requested_name_raises():
    with pytest.raises(UnknownToolError, match="No such tool"):
        asyncio.run(_binding(["short_tool"]).get_client_tools(["missing"]))
