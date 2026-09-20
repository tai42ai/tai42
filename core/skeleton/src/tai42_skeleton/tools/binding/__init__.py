"""Tool-binding engine — the impl body behind the ``app.tools`` facet.

Owns every tool/toolkit/remote-MCP binding path: manifest gating, extension
stacking, FastMCP registration, lookup, and direct invocation. State that the
lifecycle swaps on every ``start()`` (manifest, registries, the FastMCP server)
is read through the owning app so a reload is always visible here.
"""

from fastmcp.tools.function_tool import FunctionTool

from tai42_skeleton.tools.binding.client_tools import CLIENT_TOOL_NAME_MAX_LEN
from tai42_skeleton.tools.binding.errors import UnknownToolError
from tai42_skeleton.tools.binding.facet import ToolBinding

# ``FunctionTool`` is the concrete tool type the dispatch and registration seams
# resolve against; it is re-exported so ``app.tools``'s public module surface
# names the type its callers check bound tools against.
__all__ = ["CLIENT_TOOL_NAME_MAX_LEN", "FunctionTool", "ToolBinding", "UnknownToolError"]
