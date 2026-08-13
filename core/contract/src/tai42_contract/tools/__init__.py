"""Tools contract: the ``ToolInfo`` model + the ``AppTools`` tool/toolkit
registration sub-protocol. Vendor return types (fastmcp ``Tool``, langchain
``StructuredTool``) are ``TYPE_CHECKING``-only.

``AppTools`` is the ``app.tools`` namespace of the assembled facade
(:mod:`tai42_contract.app`)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, overload, runtime_checkable

from pydantic import BaseModel

from tai42_contract.manifest import ExtensionElement

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


__all__ = ["AppTools", "ToolInfo", "ToolRefsExtractor"]
