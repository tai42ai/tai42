"""LangChain / FastMCP / MCP tool glue: lc, signature.

The signature helpers ride the base install (fastmcp is a core dependency) and
are imported eagerly. The lc_util re-exports (``mcp_tool_to_lc_tool`` /
``mcp_tools_to_lc_tools``) pull ``langchain_core``, which is opt-in via the
``llm`` extra, so they resolve lazily on first attribute access — the package
(and ``tai42_kit.utils.lc.signature_util``) imports without the LangChain stack,
and touching an lc_util name without it raises a ``ModuleNotFoundError`` naming
the ``tai42-kit[llm]`` extra.
"""

from importlib import import_module
from typing import TYPE_CHECKING

from tai42_kit.utils.lc.signature_util import (
    add_signature_params,
    exclude_fastmcp_ctx_from_kwargs,
)

if TYPE_CHECKING:
    from tai42_kit.utils.lc.lc_util import mcp_tool_to_lc_tool, mcp_tools_to_lc_tools

__all__ = [
    "add_signature_params",
    "exclude_fastmcp_ctx_from_kwargs",
    "mcp_tool_to_lc_tool",
    "mcp_tools_to_lc_tools",
]

_LC_UTIL_EXPORTS = ("mcp_tool_to_lc_tool", "mcp_tools_to_lc_tools")


def __dir__() -> list[str]:
    # The lazy lc_util re-exports live outside the module namespace until first
    # access; list them alongside it so dir() shows the full public surface.
    return sorted(set(globals()) | set(_LC_UTIL_EXPORTS))


def __getattr__(name: str) -> object:
    if name not in _LC_UTIL_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        lc_util = import_module("tai42_kit.utils.lc.lc_util")
    except ModuleNotFoundError as exc:
        # Only a missing langchain distribution gets the extra hint; any other
        # missing module is a real defect and propagates untouched.
        if exc.name is not None and exc.name.partition(".")[0].startswith("langchain"):
            raise ModuleNotFoundError(
                f"{__name__}.{name} needs the LangChain stack; install the 'llm' extra "
                f"(tai42-kit[llm]) to use it. Missing module: {exc.name!r}.",
                name=exc.name,
            ) from exc
        raise
    return getattr(lc_util, name)
