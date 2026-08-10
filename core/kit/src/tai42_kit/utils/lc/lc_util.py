import mcp
from fastmcp.client import Client
from langchain_core.tools import StructuredTool

from tai42_kit.clients.settings import mcp_client_settings


async def mcp_tools_to_lc_tools(client: Client, tools_names: list[str] | None = None) -> list[StructuredTool]:
    """
    Convert tools from the MCP client to LC tools.

    If `tools_names` is provided, only tools with matching names are included,
    and every requested name must exist on the server — a requested-but-absent
    name raises ``ValueError`` (naming the missing tools) rather than being
    silently dropped. If `tools_names` is None or empty, all tools are returned.
    """

    name_filter = set(tools_names) if tools_names else None
    tools = await client.list_tools()

    if name_filter is not None:
        available = {tool.name for tool in tools}
        missing = name_filter - available
        if missing:
            raise ValueError(
                f"Requested MCP tool(s) not found on the server: {sorted(missing)}. "
                f"Available tools: {sorted(available)}"
            )

    return [mcp_tool_to_lc_tool(client, tool) for tool in tools if name_filter is None or tool.name in name_filter]


def mcp_tool_to_lc_tool(client: Client, mcp_tool: mcp.types.Tool) -> StructuredTool:
    # Names map 1:1 to the filter key and the LLM tool-name limit; raise on an
    # over-length name rather than silently truncating it (which would desync the
    # registered name from the filter key).
    if len(mcp_tool.name) > 64:
        raise ValueError(
            f"MCP tool name exceeds the 64-character limit: {mcp_tool.name!r} ({len(mcp_tool.name)} chars)"
        )

    def coroutine_impl(tool):
        async def call_tool(**kwargs):
            # Bound the downstream call so an accepted-then-stalled MCP tool can't
            # hang the caller forever; fastmcp raises its own timeout error, which
            # propagates untouched (loud).
            return await client.call_tool(tool.name, kwargs, timeout=mcp_client_settings().call_timeout_seconds)

        return call_tool

    return StructuredTool.from_function(
        func=None,
        coroutine=coroutine_impl(mcp_tool),
        name=mcp_tool.name,
        description=mcp_tool.description,
        args_schema=mcp_tool.inputSchema,
    )
