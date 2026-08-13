"""Tools feature impl behind ``tai42_contract.tools``.

Holds the ``ToolRegistry`` (the requested-tool store keyed by BASE tool name,
each mapped to its structured extension combos, backing the app facade's
``register_tool_info`` / ``unregister_tool_info`` surface), the
``ToolRefsRegistry`` (a base tool's declared composed-tool-names extractor), and
the tool-dispatch adapters that turn a vendor tool into a callable.
"""

from tai42_skeleton.tools.adapters import (
    lc_tool_to_func,
    mcp_tool_call_wrapper,
    mcp_tool_to_func,
)
from tai42_skeleton.tools.registry import ToolRegistry
from tai42_skeleton.tools.tool_refs import ToolRefsRegistry

__all__ = [
    "ToolRefsRegistry",
    "ToolRegistry",
    "lc_tool_to_func",
    "mcp_tool_call_wrapper",
    "mcp_tool_to_func",
]
