"""A stdio MCP server child writes its stderr to this process's standard error descriptor.

The child is launched with file descriptor 2 whatever object stands at ``sys.stderr``,
so a host that replaces ``sys.stderr`` with a stream that has no file descriptor (a task
worker's logging proxy) still launches the child, and the child's stderr still reaches
the process's standard error.
"""

import sys

import pytest
from fastmcp import Client
from tai42_contract.manifest import MCPConfig, TaiMCPConfig

from tai42_kit.transport import get_mcp_transport

_SERVER = """
import sys
from fastmcp import FastMCP

print("stdio-child-stderr-marker", file=sys.stderr, flush=True)
server = FastMCP("probe")


@server.tool
def ping() -> str:
    return "pong"


server.run(show_banner=False)
"""


class _NoDescriptorStream:
    """A text stream with no file descriptor, as a task worker's logging proxy is."""

    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        return None


def _server_config() -> TaiMCPConfig:
    return TaiMCPConfig(title="probe", config=MCPConfig(command=sys.executable, args=["-c", _SERVER]))


async def test_a_stdio_child_launches_and_lists_its_tools_when_sys_stderr_has_no_descriptor(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
):
    replaced = _NoDescriptorStream()
    monkeypatch.setattr(sys, "stderr", replaced)

    async with Client(get_mcp_transport(_server_config())) as client:
        tools = await client.list_tools()

    assert [tool.name for tool in tools] == ["ping"]
    assert "stdio-child-stderr-marker" in capfd.readouterr().err
    assert not any("stdio-child-stderr-marker" in text for text in replaced.written)


async def test_a_stdio_child_writes_its_stderr_to_the_process_standard_error(capfd: pytest.CaptureFixture[str]):
    async with Client(get_mcp_transport(_server_config())) as client:
        await client.list_tools()

    assert "stdio-child-stderr-marker" in capfd.readouterr().err
