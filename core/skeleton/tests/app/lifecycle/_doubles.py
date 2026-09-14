"""Shared network-free doubles and config factory for the lifecycle-mixin tests."""

from __future__ import annotations

from typing import Any, ClassVar, cast

from tai42_contract.manifest import MCPConfig, TaiMCPConfig

from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin
from tai42_skeleton.app.server import ServingCore


class _FakeMcpTool:
    name = "ping"
    description = "ping"
    inputSchema: ClassVar[dict] = {"type": "object", "properties": {}}
    outputSchema: ClassVar[dict] = {}


class _NoManifestConfig:
    """Embedded/test runtime with no external manifest file: ``read_manifest``
    raises ``FileNotFoundError`` so ``_refresh_manifest_mcp`` keeps its in-memory
    rows."""

    def read_manifest(self):
        raise FileNotFoundError("no external manifest")


class _StubPresetManager:
    """A no-op preset manager: the network-free ``_Mixin`` binds no presets, so the
    post-reload/deregister reconciliation has nothing to do. Records the calls so a
    test can assert the reconciliation ran."""

    def __init__(self) -> None:
        self.reconciled: list[set[str]] = []

    async def reconcile_bases(self, affected_bases: set[str]) -> None:
        self.reconciled.append(set(affected_bases))


class _Mixin(TaiMCPLifecycleMixin):
    """A concrete-enough mixin: ``_mcp_tools`` records bound tool names without a
    real server."""

    def __init__(self):
        super().__init__()
        self._config_manager = _NoManifestConfig()  # pyright: ignore[reportAttributeAccessIssue]
        self.preset_manager = cast("Any", _StubPresetManager())
        # A minimal serving core so the per-epoch forwarding reads (``_fast_mcp`` &c)
        # resolve without a full ``TaiMCP``; the network-free tests only touch its
        # FastMCP surface.
        self._building = ServingCore(cast("Any", self), args=(), auth=None, kwargs={"name": "mixin-under-test"})

    def _build_serving_core(self) -> ServingCore:
        return ServingCore(cast("Any", self), args=(), auth=None, kwargs={"name": "mixin-under-test"})

    def _mcp_tools(self, config, tools):
        self._mcp_bound_tools[config.title] = {f"{config.title}_t"}


def _cfg(title="svc"):
    return TaiMCPConfig(title=title, include=[], config=MCPConfig(type="http", url="http://x/mcp"))
