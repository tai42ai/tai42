"""``ToolTierFenceMiddleware``: the run-time tier fence at the MCP tool-call edge.

An MCP ``tools/call`` reaches ``Tool.run`` directly, never the ``ToolBinding.run_tool``
seam that fences the in-process doors, so the fence is enforced again at this edge. This
covers its deny/allow/pass-through behavior and its install on the main server and every
sub-MCP mount (the door test for the one door that bypasses the shared seam).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext
from tai42_contract.app import RouteAction

import tai42_skeleton.operations._authority as authority
from tai42_skeleton.tools.tier import ToolTierFenceMiddleware, ToolTierRegistry


@dataclass
class _Caller:
    is_admin: bool


@dataclass
class _Msg:
    name: str


@dataclass
class _Ctx:
    message: _Msg


class _FakeTools:
    def __init__(self, registry: ToolTierRegistry, base_map: dict[str, str]) -> None:
        self._registry = registry
        self._base_map = base_map

    def tier(self, base_tool: str):
        return self._registry.get(base_tool)

    def base_of(self, name: str) -> str:
        return self._base_map.get(name, name)


class _FakePresetManager:
    def __init__(self, specs: dict[str, str]) -> None:
        self._specs = specs

    def is_registered(self, name: str) -> bool:
        return name in self._specs

    def get_spec(self, name: str) -> MagicMock:
        spec = MagicMock()
        spec.base_tool = self._specs[name]
        return spec


class _FakeApp:
    def __init__(
        self,
        tiers: dict[str, RouteAction],
        presets: dict[str, str] | None = None,
        base_map: dict[str, str] | None = None,
    ) -> None:
        registry = ToolTierRegistry()
        for name, tier in tiers.items():
            registry.register(name, tier)
        self._registration_tier_registry = registry
        self.tools = _FakeTools(registry, base_map or {})
        self.preset_manager = _FakePresetManager(presets or {})


async def _call(mw: ToolTierFenceMiddleware, name: str) -> tuple[bool, Any]:
    reached = {"v": False}

    async def call_next(_ctx: Any) -> str:
        reached["v"] = True
        return "reached"

    result = await mw.on_call_tool(cast("MiddlewareContext[Any]", _Ctx(_Msg(name))), call_next)
    return reached["v"], result


@pytest.fixture
def non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _caller() -> _Caller:
        return _Caller(is_admin=False)

    monkeypatch.setattr(authority, "resolve_caller", _caller)


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _caller() -> _Caller:
        return _Caller(is_admin=True)

    monkeypatch.setattr(authority, "resolve_caller", _caller)


async def test_fenced_tool_denied_for_non_admin(non_admin: None) -> None:
    mw = ToolTierFenceMiddleware(cast("Any", _FakeApp({"sandbox_exec": "fenced"})))
    with pytest.raises(ToolError) as exc:
        await _call(mw, "sandbox_exec")
    message = str(exc.value)
    assert "sandbox_exec" in message
    assert "fenced" in message


async def test_fenced_tool_admitted_for_admin(admin: None) -> None:
    mw = ToolTierFenceMiddleware(cast("Any", _FakeApp({"sandbox_exec": "fenced"})))
    reached, result = await _call(mw, "sandbox_exec")
    assert reached is True
    assert result == "reached"


async def test_preset_over_fenced_base_denied_for_non_admin(non_admin: None) -> None:
    app = _FakeApp({"sandbox_exec": "fenced"}, presets={"deploy_env": "sandbox_exec"})
    mw = ToolTierFenceMiddleware(cast("Any", app))
    with pytest.raises(ToolError):
        await _call(mw, "deploy_env")


async def test_unfenced_tool_passes_through_without_a_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom() -> _Caller:
        raise AssertionError("resolve_caller must not be consulted for an unfenced tool")

    monkeypatch.setattr(authority, "resolve_caller", _boom)
    mw = ToolTierFenceMiddleware(cast("Any", _FakeApp({"notes": "write"})))
    reached, result = await _call(mw, "notes")
    assert reached is True
    assert result == "reached"


def test_fence_middleware_installed_on_main_server() -> None:
    from tai42_skeleton.app.instance import app

    assert any(isinstance(m, ToolTierFenceMiddleware) for m in app._fast_mcp.middleware)


async def test_fence_middleware_installed_on_sub_mcp_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every sub-MCP FastMCP built by ``_build_sub_app`` re-adds the fence (the main
    server's middleware never reaches a sub-mount)."""
    from starlette.applications import Starlette

    import tai42_skeleton.app.sub_mcp_app as sub_mod
    from tai42_skeleton.app.instance import app
    from tai42_skeleton.app.sub_mcp_app import SubMcpAppRouter
    from tai42_skeleton.manifest import Manifest

    instances: list = []
    real_fastmcp = sub_mod.FastMCP

    class _RecordingFastMCP(real_fastmcp):
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, **k)
            instances.append(self)

    monkeypatch.setattr(sub_mod, "FastMCP", _RecordingFastMCP)

    manifest = Manifest.model_validate(
        {"tools": [{"title": "fxt", "module": "tests.app._fixtures.tools_a", "include": ["greet"]}]}
    )

    async with app.app_context(manifest):
        router = cast("SubMcpAppRouter", app.sub_app.mcp_sub_app_router)
        async with router.lifespan(cast("Starlette", None)):
            await router.register_sub_mcp_app("http_svc", ["greet"], transport="http")
            built = await router._get_or_build_app("http_svc")
            assert built is not None

    assert instances, "no sub-MCP FastMCP was built"
    assert any(any(isinstance(m, ToolTierFenceMiddleware) for m in inst.middleware) for inst in instances), (
        "ToolTierFenceMiddleware not installed on any sub-MCP app"
    )
