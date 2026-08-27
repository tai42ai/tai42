"""Tools contract: the ``ToolInfo`` model + the ``AppTools`` tool/toolkit
registration sub-protocol. Vendor return types (fastmcp ``Tool``, langchain
``StructuredTool``) are ``TYPE_CHECKING``-only.

``AppTools`` is the ``app.tools`` namespace of the assembled facade
(:mod:`tai42_contract.app`)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, overload, runtime_checkable

from pydantic import BaseModel

from tai42_contract.manifest import ExtensionElement
from tai42_contract.tools.invocation import (
    ToolInvocation,
    current_tool_invocation,
    reset_current_tool_invocation,
    set_current_tool_invocation,
)

if TYPE_CHECKING:
    from fastmcp.tools import Tool
    from langchain_core.tools import StructuredTool

# Preserves the decorated callable's type through ``tool`` / ``toolkit`` so
# ``@app.tools.tool`` keeps the wrapped function's signature instead of ``Any``.
F = TypeVar("F", bound=Callable[..., Any])

#: A base tool's declared tool-references extractor: given a preset's baked
#: ``fixed_kwargs``, returns the tool names a preset of THIS base composes. The
#: platform never knows a base tool's config shape — the base tool declares how to
#: read composed tool names out of its own ``fixed_kwargs``. Every entry must be a
#: string; a non-string / None entry is a plugin bug the reader raises on.
ToolRefsExtractor = Callable[[dict[str, Any]], Iterable[str]]

#: A rename referee: given the tool name about to be renamed, returns
#: human-readable descriptions of every live reference that would be stranded by
#: the rename (empty = no objection). A referee raising is a hard failure of the
#: rename — never a silent bypass. The platform gathers every registered
#: referee's answers and blocks the rename when any is non-empty.
ToolRenameReferee = Callable[[str], Awaitable[list[str]]]


class ToolInfo(BaseModel):
    """Descriptor for a registered tool.

    ``name`` is the registered key; ``base`` is the underlying tool it was
    bound from (a branch tool names itself, e.g. ``name_chain``).
    """

    name: str
    base: str = ""
    title: str = ""
    description: str = ""


@runtime_checkable
class AppTools(Protocol):
    """Tool + toolkit registration / lookup surface."""

    # Bare ``@app.tools.tool`` decorates the function directly (returns it
    # unchanged); parameterized ``@app.tools.tool(...)`` returns the decorator.
    # Both keep the wrapped function's type. ``tool_refs`` is the optional declared
    # extractor for the composed tool names a preset of this base tool carries in
    # its baked ``fixed_kwargs`` (registered only when the manifest includes the
    # tool).
    @overload
    def tool(self, func: F, /) -> F: ...
    @overload
    def tool(
        self, *args: Any, force: bool = False, tool_refs: ToolRefsExtractor | None = None, **kwargs: Any
    ) -> Callable[[F], F]: ...

    @overload
    def toolkit(self, target: F, /) -> F: ...
    @overload
    def toolkit(self, *args: Any, **kwargs: Any) -> Callable[[F], F]: ...

    def tool_title(self, func: Callable[..., object]) -> str: ...

    async def get_tool(self, key: str) -> Tool: ...

    async def get_tools(self) -> dict[str, Tool]: ...

    async def get_client_tools(self, names: list[str] | None = None) -> list[StructuredTool]: ...

    async def run_tool(self, key: str, arguments: dict[str, Any], *, offload_sync: bool = False) -> Any: ...

    def remove_tool(self, name: str) -> None: ...

    # ``combos`` is the tool's list of extension combos (each combo a stack of
    # extension elements — a bare name or a ``{"name", "config"}`` mapping);
    # ``register`` attaches them to the base ``name``.
    def register_tool_info(self, name: str, combos: Sequence[Sequence[ExtensionElement]] | None = None) -> None: ...

    def unregister_tool_info(self, name: str) -> None: ...

    def unregister_tool_base(self, tool_name: str) -> list[str]: ...

    # The declared tool-references extractor a base tool registered under ``name``,
    # or ``None`` when it declared none — the preset reference collector consults it
    # for a body's ``base_tool``.
    def tool_refs_extractor(self, name: str) -> ToolRefsExtractor | None: ...

    def register_rename_referee(self, provider: ToolRenameReferee) -> None:
        """Register a :data:`ToolRenameReferee` consulted before a tool rename.

        A plugin holding tool-name references calls this through the ``tai42_app``
        handle when its module loads. Every registered referee is asked for the
        old name on a rename; any non-empty answer blocks the rename and its
        descriptions name the holders. Registering the same provider object twice
        raises loudly — a double registration is a plugin bug, never a silent
        duplicate consult."""
        ...


__all__ = [
    "AppTools",
    "ToolInfo",
    "ToolInvocation",
    "ToolRefsExtractor",
    "ToolRenameReferee",
    "current_tool_invocation",
    "reset_current_tool_invocation",
    "set_current_tool_invocation",
]
