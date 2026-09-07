"""The run-time tier fence at the shared in-process tool-run seam
(``ToolBinding.run_tool``).

A ``fenced``/``secret`` tool — or a preset or extension branch over one — runs only for an
administrator; ``read``/``write`` and undeclared tools carry no execution gate. The fence
lives ONCE at this chokepoint, which every in-process door funnels through (the sync HTTP
run-tool op, background submit, hook/trigger dispatch, the agent run-tool binding,
schedules, channel turns, chain/batch re-dispatch), so driving ``run_tool`` directly with a
fenced tool and a stubbed caller proves the fence for all of them. Two doors reach the tool
BODY without this seam and are covered separately: the MCP edge
(``test_run_tier_fence_mcp.py``) and the agent tool-dispatch door
(``_client_runnable``, the langchain client tool an in-process agent invokes — covered by
``test_client_runnable_*`` below).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import cast
from unittest.mock import MagicMock

import pytest
from tai42_contract.app import RouteAction

import tai42_skeleton.operations._authority as authority
from tai42_skeleton.app import server as server_module
from tai42_skeleton.app.instance import app as process_app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations.errors import ForbiddenError
from tai42_skeleton.tools import binding as binding_module
from tai42_skeleton.tools.binding import ToolBinding
from tai42_skeleton.tools.retry import ToolRetryRegistry
from tai42_skeleton.tools.tier import ToolTierRegistry


@dataclass
class _Caller:
    is_admin: bool


def _async_return(value):
    async def _coro(*_args, **_kwargs):
        return value

    return _coro


def _function_tool(fn):
    tool = MagicMock(spec=binding_module.FunctionTool)
    tool.fn = fn
    return tool


def _echo(x: int = 0) -> int:
    return x


def _binding(
    *,
    tiers: dict[str, RouteAction] | None = None,
    presets: dict[str, str] | None = None,
    base_map: dict[str, str] | None = None,
) -> ToolBinding:
    """A ``ToolBinding`` whose tier registry, preset resolution and ``base_of`` are wired to
    real behavior so the fence resolves a run key to its base tool's tier exactly as in
    production.

    ``tiers`` maps base tool -> RouteAction, ``presets`` maps preset name -> base tool,
    ``base_map`` maps a branch tool -> its origin base.
    """
    app_mock = MagicMock(spec=server_module.TaiMCP)
    registry = ToolTierRegistry()
    for name, tier in (tiers or {}).items():
        registry.register(name, tier)
    app_mock._registration_tier_registry = registry
    app_mock._tool_retry_registry = ToolRetryRegistry()
    app_mock.tools.tier.side_effect = registry.get
    base_map = base_map or {}
    app_mock.tools.base_of.side_effect = lambda name: base_map.get(name, name)
    presets = presets or {}
    app_mock.preset_manager.is_registered.side_effect = lambda name: name in presets
    # A preset key resolves to its base tool; a run of one never enters the preset
    # attribution path here (version None → plain dispatch), so the fence, not the
    # runs-index machinery, is what these tests exercise.
    app_mock.preset_manager.active_version.side_effect = lambda _name: None

    def _get_spec(name: str) -> MagicMock:
        spec = MagicMock()
        spec.base_tool = presets[name]
        return spec

    app_mock.preset_manager.get_spec.side_effect = _get_spec
    binding = ToolBinding(app_mock)
    binding.get_tool = _async_return(_function_tool(_echo))  # type: ignore[method-assign]
    return binding


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


def _no_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom() -> _Caller:
        raise AssertionError("resolve_caller must not be consulted for an unfenced tool")

    monkeypatch.setattr(authority, "resolve_caller", _boom)


async def test_fenced_tool_refused_for_non_admin(non_admin: None) -> None:
    binding = _binding(tiers={"sandbox_exec": "fenced"})
    with pytest.raises(ForbiddenError) as exc:
        await binding.run_tool("sandbox_exec", {"x": 1})
    message = str(exc.value)
    assert "sandbox_exec" in message
    assert "fenced" in message


async def test_fenced_tool_admitted_for_admin(admin: None) -> None:
    binding = _binding(tiers={"sandbox_exec": "fenced"})
    assert await binding.run_tool("sandbox_exec", {"x": 3}) == 3


async def test_secret_tool_refused_for_non_admin(non_admin: None) -> None:
    binding = _binding(tiers={"vault_read": "secret"})
    with pytest.raises(ForbiddenError) as exc:
        await binding.run_tool("vault_read", {"x": 2})
    assert "secret" in str(exc.value)


@pytest.mark.parametrize("tier", ["read", "write", None])
async def test_unfenced_tiers_run_without_resolving_a_caller(monkeypatch: pytest.MonkeyPatch, tier: str | None) -> None:
    _no_caller(monkeypatch)
    tiers: dict[str, RouteAction] = {"echo": cast("RouteAction", tier)} if tier is not None else {}
    binding = _binding(tiers=tiers)
    assert await binding.run_tool("echo", {"x": 9}) == 9


async def test_preset_over_fenced_base_is_fenced_for_non_admin(non_admin: None) -> None:
    binding = _binding(tiers={"sandbox_exec": "fenced"}, presets={"deploy_env": "sandbox_exec"})
    with pytest.raises(ForbiddenError) as exc:
        await binding.run_tool("deploy_env", {"x": 1})
    # Named by the RUN key (the preset); fenced because its BASE tool is fenced.
    assert "deploy_env" in str(exc.value)


async def test_preset_over_write_base_runs_for_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_caller(monkeypatch)
    binding = _binding(tiers={"notes": "write"}, presets={"note_it": "notes"})
    assert await binding.run_tool("note_it", {"x": 4}) == 4


async def test_extension_branch_over_fenced_base_is_fenced_for_non_admin(non_admin: None) -> None:
    binding = _binding(tiers={"sandbox_exec": "fenced"}, base_map={"sandbox_exec_chain": "sandbox_exec"})
    with pytest.raises(ForbiddenError):
        await binding.run_tool("sandbox_exec_chain", {"x": 1})


def test_binding_helpers_typed() -> None:
    # ``_binding`` returns a callable-driving ToolBinding; a light guard so the helper's
    # contract (get_tool stubbed to a FunctionTool) stays intact under refactors.
    binding = _binding(tiers={"sandbox_exec": "fenced"})
    assert isinstance(binding, ToolBinding)
    assert isinstance(binding.get_tool, Callable)


def test_tier_decorator_registers_like_the_programmatic_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # The declarative ``@app.tools.tool(tier=...)`` form registers the tier into the same
    # registry the programmatic ``register_tier`` writes, keyed by the bound tool name.
    app_mock = MagicMock(spec=server_module.TaiMCP)
    registry = ToolTierRegistry()
    app_mock._registration_tier_registry = registry
    app_mock._manifest.should_include_tool.return_value = True
    binding = ToolBinding(app_mock)
    monkeypatch.setattr(binding, "bind_tool_func", lambda *_a, **_k: lambda fn: fn)

    @binding.tool(tier="fenced", name="risky")
    def risky() -> int:
        return 1

    assert registry.get("risky") == "fenced"


def test_tier_registry_rejects_duplicate_registration() -> None:
    # A base tool declares its tier once; a duplicate raises loudly rather than silently
    # swapping a tool's authorization character out from under the gate and the fence.
    registry = ToolTierRegistry()
    registry.register("t", "fenced")
    with pytest.raises(ValueError, match="already registered"):
        registry.register("t", "read")


def test_tier_decorator_skips_a_manifest_excluded_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    # A tool the manifest excludes registers nothing — the declaration follows inclusion,
    # exactly like the retry / tool_refs declarations.
    app_mock = MagicMock(spec=server_module.TaiMCP)
    registry = ToolTierRegistry()
    app_mock._registration_tier_registry = registry
    app_mock._manifest.should_include_tool.return_value = False
    binding = ToolBinding(app_mock)
    monkeypatch.setattr(binding, "bind_tool_func", lambda *_a, **_k: lambda fn: fn)

    @binding.tool(tier="fenced", name="risky")
    def risky() -> int:
        return 1

    assert registry.get("risky") is None


# -- The agent tool-dispatch door (``_client_runnable``) -----------------------------------
#
# ``get_client_tools`` builds a langchain ``StructuredTool`` whose runnable
# (:meth:`ToolBinding._client_runnable`) reaches the tool BODY directly, never the
# ``run_tool`` seam, so it enforces the fence itself. These drive a real app + a fenced tool
# through that runnable, so the door is proven against the actual dispatch path an in-process
# agent uses.


@pytest.fixture
def clean_process_server() -> Iterator[None]:
    async def _clear() -> None:
        provider = process_app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


@pytest.mark.usefixtures("clean_process_server")
def test_client_runnable_fences_a_fenced_tool_for_non_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _non_admin() -> _Caller:
        return _Caller(is_admin=False)

    monkeypatch.setattr(authority, "resolve_caller", _non_admin)

    async def run() -> None:
        async with process_app.app_context(Manifest.model_validate({})):

            @process_app.tools.tool(force=True, tier="fenced")
            async def fenced_client_probe(q: str) -> str:
                """A fenced probe body — reached only past the run-time fence."""
                return q

            tool_obj = await process_app.tools.get_tool("fenced_client_probe")
            runnable = process_app._tool_binding._client_runnable(tool_obj)
            with pytest.raises(ForbiddenError):
                await runnable(q="x")

    asyncio.run(run())


@pytest.mark.usefixtures("clean_process_server")
def test_client_runnable_admits_a_fenced_tool_for_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _admin() -> _Caller:
        return _Caller(is_admin=True)

    monkeypatch.setattr(authority, "resolve_caller", _admin)

    async def run() -> None:
        async with process_app.app_context(Manifest.model_validate({})):

            @process_app.tools.tool(force=True, tier="fenced")
            async def fenced_client_probe_admin(q: str) -> str:
                """A fenced probe body — the admin runs it past the fence."""
                return q

            tool_obj = await process_app.tools.get_tool("fenced_client_probe_admin")
            runnable = process_app._tool_binding._client_runnable(tool_obj)
            assert await runnable(q="ok") == "ok"

    asyncio.run(run())
