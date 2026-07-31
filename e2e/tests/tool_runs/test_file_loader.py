"""C7 — the ``file_loader`` builtin tool loads a resource from a storage id and
from an http(s) url and returns its extracted text.

Drives the real tool through the real MCP door on ``core_stack`` (which mounts the
tool and registers the active storage provider): a storage-id source is read back
through the provider, and a loopback-url source is fetched through the SSRF-pinned
url guard (the ``127.0.0.0/8`` range the base env allows). Both assert the loaded
text against exactly what was seeded/served — the tool actually parsed the bytes."""

from __future__ import annotations

from collections.abc import Callable

from fastmcp.client.client import CallToolResult

from tai42_e2e.netfixtures import TargetServer
from tai42_e2e.stack import TaiStack


def _result_text(result: CallToolResult) -> str:
    """The concatenated text of an MCP result's content blocks. ``file_loader``
    returns ``str | MediaBlock`` — a union carries no structured-output schema, so
    the loaded text rides text content blocks (``result.data`` is ``None``)."""
    return "".join(getattr(block, "text", "") for block in (result.content or []))


async def test_file_loader_reads_storage_resource(core_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = core_stack.api()
    resource_id = f"{uniq('doc')}.txt"
    content = f"The storage document body marker is {uniq('smark')}."

    stored = await api.post("/api/storage/resources", json={"id": resource_id, "content_text": content})
    assert stored == {"id": resource_id, "stored": True}

    async with core_stack.mcp() as mcp:
        result = await mcp.call_tool("file_loader", {"source": resource_id})

    text = _result_text(result)
    assert content in text, f"file_loader did not return the seeded text: {text!r}"


async def test_file_loader_fetches_loopback_url(
    core_stack: TaiStack, target_server: TargetServer, uniq: Callable[[str], str]
) -> None:
    marker = uniq("umark")
    url = f"{target_server.url}/doc.txt?body={marker}"
    before = sum(1 for record in target_server.records if record.path == "/doc.txt")

    async with core_stack.mcp() as mcp:
        result = await mcp.call_tool("file_loader", {"source": url})

    text = _result_text(result)
    assert marker in text, f"file_loader did not return the served text: {text!r}"
    after = sum(1 for record in target_server.records if record.path == "/doc.txt")
    assert after == before + 1, "the guarded fetch never reached the target server"
