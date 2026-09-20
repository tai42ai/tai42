"""The ``ToolBinding`` composition root.

The owning app reference, the live-app-state accessors the lifecycle swaps on every start, and the
manifest requirement guard.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from tai42_skeleton.app.server import TaiMCP
    from tai42_skeleton.extensions import ExtensionRegistry
    from tai42_skeleton.manifest import Manifest
    from tai42_skeleton.tools.registry import ToolRegistry
    from tai42_skeleton.tools.retry import ToolRetryRegistry
    from tai42_skeleton.tools.tier import ToolTierRegistry
    from tai42_skeleton.tools.tool_refs import ToolRefsRegistry


class _ToolBindingBase:
    """Binds tools onto the app's live FastMCP server.

    Holds no lifecycle state of its own — manifest/registries/server are
    properties over the owning app, which rebuilds them on every start/reload.
    """

    def __init__(self, app: "TaiMCP") -> None:
        self._app = app

    # -- live app state (swapped by the lifecycle on every start) -------------

    @property
    def _fast_mcp(self) -> "FastMCP":
        return self._app._fast_mcp

    @property
    def _manifest(self) -> "Manifest | None":
        return self._app._manifest

    @property
    def _tool_registry(self) -> "ToolRegistry":
        return self._app._tool_registry

    @property
    def _extension_registry(self) -> "ExtensionRegistry":
        return self._app._extension_registry

    @property
    def _mcp_bound_tools(self) -> dict[str, set[str]]:
        return self._app._mcp_bound_tools

    @property
    def _tool_refs_registry(self) -> "ToolRefsRegistry":
        return self._app._tool_refs_registry

    @property
    def _tool_retry_registry(self) -> "ToolRetryRegistry":
        return self._app._tool_retry_registry

    @property
    def _registration_tier_registry(self) -> "ToolTierRegistry":
        return self._app._registration_tier_registry

    def _require_manifest(self) -> "Manifest":
        manifest = self._manifest
        if manifest is None:
            raise RuntimeError("TaiMCP is not started — call start()/app_context first.")
        return manifest
