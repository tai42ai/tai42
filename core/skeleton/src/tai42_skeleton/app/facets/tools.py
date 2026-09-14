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
        return self._app._tool_binding.tool(*args, force=force, **kwargs)

    def toolkit(self, *args, **kwargs):
        return self._app._tool_binding.toolkit(*args, **kwargs)

    def tool_title(self, func) -> str:
        return self._app._tool_binding.tool_title(func)

    async def get_tool(self, key: str) -> Tool:
        return await self._app._tool_binding.get_tool(key)

    async def get_tools(self) -> dict[str, Tool]:
        return await self._app._tool_binding.get_tools()

    async def get_client_tools(self, names: list[str] | None = None) -> list[StructuredTool]:
        return await self._app._tool_binding.get_client_tools(names)

    async def run_tool(self, key: str, arguments: dict[str, Any], *, offload_sync: bool = False) -> Any:
        return await self._app._tool_binding.run_tool(key, arguments, offload_sync=offload_sync)

    def remove_tool(self, name: str) -> None:
        return self._app._tool_binding.remove_tool(name)

    def register_tool_info(self, name: str, combos: Sequence[Sequence[ExtensionElement]] | None = None):
        return self._app._tool_binding.register_tool_info(name, combos)

    def unregister_tool_info(self, name: str):
        return self._app._tool_binding.unregister_tool_info(name)

    def unregister_tool_base(self, tool_name: str) -> list[str]:
        return self._app._tool_binding.unregister_tool_base(tool_name)

    def tool_refs_extractor(self, name: str) -> ToolRefsExtractor | None:
        """The declared tool-references extractor a base tool registered under
        ``name``, or ``None`` when it declared none."""
        return self._app._tool_binding.tool_refs_extractor(name)

    def base_of(self, name: str) -> str:
        """The base tool ``name`` was produced from (``name`` itself for a base or
        an unbound name; the origin base for an extension branch)."""
        return self._app._tool_binding.base_of(name)

    def is_branch(self, name: str) -> bool:
        """Whether ``name`` is an extension branch tool rather than a base."""
        return self._app._tool_binding.is_branch(name)

    def mcp_bound_names(self, title: str) -> frozenset[str]:
        """A read-only snapshot of the tool names the MCP server ``title`` currently
        binds (empty for an unknown title)."""
        return self._app._tool_binding.mcp_bound_names(title)

    def register_rename_referee(self, provider: ToolRenameReferee) -> None:
        return self._app._rename_referee_registry.register(provider)

    def rename_referees(self) -> list[ToolRenameReferee]:
        """Every registered rename referee (plugin providers + platform-internal
        wiring). Skeleton-only — the rename gate and the referees preview door consult
        it, so it is not on the ``AppTools`` protocol (the register-only seam), the same
        precedent :meth:`PresetsFacet.write_validator` sets."""
        return self._app._rename_referee_registry.all()

    def register_delete_referee(self, provider: ToolDeleteReferee) -> None:
        return self._app._delete_referee_registry.register(provider)

    def delete_referees(self) -> list[ToolDeleteReferee]:
        """Every registered delete referee (plugin providers). Skeleton-only — the preset
        delete door consults it, so it is not on the ``AppTools`` protocol's read side (the
        register-only seam), mirroring :meth:`rename_referees`."""
        return self._app._delete_referee_registry.all()

    def register_detach_referee(self, provider: StateTemplateDetachReferee) -> None:
        return self._app._detach_referee_registry.register(provider)

    def detach_referees(self) -> list[StateTemplateDetachReferee]:
        """Every registered state-template detach referee (platform-internal binding holders
        + plugin providers). Skeleton-only — the state-template detach door consults it, so it
        is not on the ``AppTools`` protocol's read side (the register-only seam), mirroring
        :meth:`delete_referees`."""
        return self._app._detach_referee_registry.all()

    def register_tier(self, base_tool: str, tier: RouteAction) -> None:
        return self._app._registration_tier_registry.register(base_tool, tier)

    def tier(self, base_tool: str) -> RouteAction | None:
        return self._app._registration_tier_registry.get(base_tool)
