"""The ``app.tools`` facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import _Facet

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Any

    from fastmcp.tools import Tool
    from langchain_core.tools import StructuredTool
    from tai42_contract.manifest import ExtensionElement
    from tai42_contract.tools import (
        StateTemplateDetachReferee,
        ToolDeleteReferee,
        ToolRefsExtractor,
        ToolRenameReferee,
    )

    from tai42_skeleton.app.route_registry import RouteAction


class ToolsFacet(_Facet):
    """``app.tools`` — tool/toolkit registration + lookup (``AppTools``)."""

    def tool(self, *args, force: bool = False, **kwargs) -> Callable[..., Any]:
        """Register a tool via decorator; ``force`` replaces an existing registration under the same name."""
        return self._app._tool_binding.tool(*args, force=force, **kwargs)

    def toolkit(self, *args, **kwargs):
        """Register a toolkit (a group of related tools registered together)."""
        return self._app._tool_binding.toolkit(*args, **kwargs)

    def tool_title(self, func) -> str:
        """The display title derived for the tool function ``func``."""
        return self._app._tool_binding.tool_title(func)

    async def get_tool(self, key: str) -> Tool:
        """The registered :class:`Tool` for ``key``."""
        return await self._app._tool_binding.get_tool(key)

    async def get_tools(self) -> dict[str, Tool]:
        """Every registered tool keyed by name."""
        return await self._app._tool_binding.get_tools()

    async def get_client_tools(self, names: list[str] | None = None) -> list[StructuredTool]:
        """The LangChain ``StructuredTool`` view of the tools named in ``names`` (all when ``None``)."""
        return await self._app._tool_binding.get_client_tools(names)

    async def run_tool(self, key: str, arguments: dict[str, Any], *, offload_sync: bool = False) -> Any:
        """Execute the tool ``key`` with ``arguments``; ``offload_sync`` runs a sync tool off the event loop."""
        return await self._app._tool_binding.run_tool(key, arguments, offload_sync=offload_sync)

    def remove_tool(self, name: str) -> None:
        """Unregister the tool named ``name``."""
        return self._app._tool_binding.remove_tool(name)

    def register_tool_info(self, name: str, combos: Sequence[Sequence[ExtensionElement]] | None = None):
        """Record extension-combo metadata for the base tool ``name``."""
        return self._app._tool_binding.register_tool_info(name, combos)

    def unregister_tool_info(self, name: str):
        """Drop the extension-combo metadata recorded for ``name``."""
        return self._app._tool_binding.unregister_tool_info(name)

    def unregister_tool_base(self, tool_name: str) -> list[str]:
        """Unregister the base tool ``tool_name`` and return the names removed with it."""
        return self._app._tool_binding.unregister_tool_base(tool_name)

    def tool_refs_extractor(self, name: str) -> ToolRefsExtractor | None:
        """The tool-references extractor the base tool ``name`` declared, or ``None`` when it declared none."""
        return self._app._tool_binding.tool_refs_extractor(name)

    def base_of(self, name: str) -> str:
        """The base tool ``name`` was produced from: ``name`` for a base/unbound name, the origin base for a branch."""
        return self._app._tool_binding.base_of(name)

    def is_branch(self, name: str) -> bool:
        """Whether ``name`` is an extension branch tool rather than a base."""
        return self._app._tool_binding.is_branch(name)

    def mcp_bound_names(self, title: str) -> frozenset[str]:
        """A read-only snapshot of the tool names the MCP server ``title`` binds (empty for an unknown title)."""
        return self._app._tool_binding.mcp_bound_names(title)

    def register_rename_referee(self, provider: ToolRenameReferee) -> None:
        """Register a rename referee that vets a tool rename before it commits."""
        return self._app._rename_referee_registry.register(provider)

    def rename_referees(self) -> list[ToolRenameReferee]:
        """Every registered rename referee (plugin providers + platform-internal wiring).

        Skeleton-only — the rename gate and the referees preview door consult it, so it is not on
        the ``AppTools`` protocol (the register-only seam), the same precedent
        :meth:`PresetsFacet.write_validator` sets.
        """
        return self._app._rename_referee_registry.all()

    def register_delete_referee(self, provider: ToolDeleteReferee) -> None:
        """Register a delete referee that vets a tool delete before it commits."""
        return self._app._delete_referee_registry.register(provider)

    def delete_referees(self) -> list[ToolDeleteReferee]:
        """Every registered delete referee (plugin providers).

        Skeleton-only — the preset delete door consults it, so it is not on the ``AppTools``
        protocol's read side (the register-only seam), mirroring :meth:`rename_referees`.
        """
        return self._app._delete_referee_registry.all()

    def register_detach_referee(self, provider: StateTemplateDetachReferee) -> None:
        """Register a state-template detach referee that vets a detach before it commits."""
        return self._app._detach_referee_registry.register(provider)

    def detach_referees(self) -> list[StateTemplateDetachReferee]:
        """Every registered state-template detach referee (platform-internal binding holders + plugin providers).

        Skeleton-only — the state-template detach door consults it, so it is not on the
        ``AppTools`` protocol's read side (the register-only seam), mirroring
        :meth:`delete_referees`.
        """
        return self._app._detach_referee_registry.all()

    def register_tier(self, base_tool: str, tier: RouteAction) -> None:
        """Bind the registration ``tier`` (route action) for ``base_tool``."""
        return self._app._registration_tier_registry.register(base_tool, tier)

    def tier(self, base_tool: str) -> RouteAction | None:
        """The registration tier bound for ``base_tool``, or ``None`` when none is bound."""
        return self._app._registration_tier_registry.get(base_tool)
