"""End-to-end: a fixture operation becomes a route + a spec entry + a projected
MCP tool that an extension wraps and ``tai tools run`` (``app.tools.run_tool``)
dispatches — and survives a reload with ``AuthzMiddleware`` intact."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from tai42_skeleton.app.instance import app
from tai42_skeleton.app.route_registry import load_api_routes
from tai42_skeleton.authz.middleware import AuthzMiddleware
from tai42_skeleton.authz.resolver import resolve_dispatch
from tai42_skeleton.cli.openapi import build_openapi_spec
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations.registry import operation_registry
from tai42_skeleton.tools.turn_budget import TurnBudgetMiddleware

from ..app._fixtures.reload import reload_with


@pytest.fixture(autouse=True)
def _clean_server():
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


def _manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "routers_modules": ["tests.operations._fixtures.sample_router"],
            "extensions_modules": ["tests.app._fixtures.ext_kinds"],
            "api_tools": {"enabled": True, "extensions": {"sample_greet": [["argswrap"]]}},
            # "none" keeps the surface to just the sample router under test.
            "default_routers": "none",
        }
    )


def test_operation_projects_to_route_spec_and_tool():
    async def run():
        async with app.app_context(_manifest()):
            # Route recorded + in the /api surface.
            assert any(r.path == "/api/sample/greet" for r in load_api_routes())
            # Spec entry emitted.
            spec = build_openapi_spec()
            assert "/api/sample/greet" in spec["paths"]
            assert "post" in spec["paths"]["/api/sample/greet"]

            tools = await app.tools.get_tools()
            # Projected tool present.
            assert "sample_greet" in tools
            # The api_tools.extensions combo attached a branch to the projected op
            # (proving projection ran BEFORE extension wraps).
            assert "sample_greet_argswrap" in tools

            # Runnable via the tool-run path.
            result = await app.tools.run_tool("sample_greet", {"name": "ann"})
            assert result == {"greeting": "hello ann"}
            branch = await app.tools.run_tool("sample_greet_argswrap", {"name": "bea"})
            assert branch == {"greeting": "hello bea"}

    asyncio.run(run())


def test_projection_and_authz_survive_reload():
    async def run():
        async with app.app_context(_manifest()):
            assert "sample_greet" in await app.tools.get_tools()
            assert any(isinstance(m, AuthzMiddleware) for m in app._fast_mcp.middleware)

            # A reload re-runs start() (projection + the resets); run it on a worker
            # thread through the gate exactly as production does.
            await reload_with(app, _manifest())

            tools = await app.tools.get_tools()
            assert "sample_greet" in tools
            assert "sample_greet_argswrap" in tools
            # The security middleware is not dropped by a reload cycle.
            assert any(isinstance(m, AuthzMiddleware) for m in app._fast_mcp.middleware)

    asyncio.run(run())


def test_disabled_api_tools_projects_empty_surface():
    """With ``api_tools.enabled`` false the projection registers no tools — the
    disabled path is an empty PROJECTION, NOT an empty registry: the registry is
    fully repopulated at boot, and only the projection is gated off.

    The live tools are the two force-registered hidden completion mechanisms the
    conversations router installs at startup (``conversation_deliver`` for a parked AGENT
    turn, ``deliver_tool_completion`` for a parked TOOL turn). They survive the api_tools
    toggle by design — mandatory bridges registered by their own startup hook and not by the
    projection — so a resumed async turn's ``run_tool`` continuation still resolves with
    api_tools off."""

    async def run():
        async with app.app_context(Manifest.model_validate({"api_tools": {"enabled": False}})):
            tools = await app.tools.get_tools()
            assert set(tools) == {"conversation_deliver", "deliver_tool_completion"}
            for name in ("conversation_deliver", "deliver_tool_completion"):
                assert (tools[name].meta or {}).get("tai42/hidden") is True
            # The registry IS populated (the boot repopulate ran); the empty PROJECTION
            # surface is the ``enabled=False`` gate, not the absence of operations.
            assert operation_registry.has("list_system_kinds")

    asyncio.run(run())


def test_disabled_api_tools_still_fires_the_completion_continuation_via_run_tool(monkeypatch):
    """A resumed async turn's completion continuation must still fire with api_tools off.

    The force-registered hidden ``conversation_deliver`` mechanism stays on the live
    surface when the projection is gated off, so ``run_tool`` — dispatched here under the
    parked run's stored (internal) execution identity, exactly as the resume driver fires
    it — resolves and dispatches it to the real body. The store's exactly-once dedup path
    (a record already committed for the completion id) returns without the delivery
    machine, keeping the focus on what is under test: the mechanism is fireable through
    ``run_tool`` with api_tools disabled. Unregistering it to empty the surface would
    strand every async turn that parked while api_tools was off."""
    import tai42_skeleton.conversations.turn as turn_module
    from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity
    from tai42_skeleton.authz.identity import INTERNAL_PRINCIPAL

    class _AlreadyCommittedStore:
        async def get_record(self, completion_id: str) -> object:
            return object()

    async def run():
        async with app.app_context(Manifest.model_validate({"api_tools": {"enabled": False}})):
            monkeypatch.setattr(turn_module, "_store", lambda: _AlreadyCommittedStore())
            token = set_execution_identity(INTERNAL_PRINCIPAL)
            try:
                out = await app.tools.run_tool(
                    "conversation_deliver",
                    {"thread_id": "bridge:line:+15550002222", "result": "hi", "completion_id": "cmpl-x"},
                )
            finally:
                reset_execution_identity(token)
            assert out == {"message_id": "cmpl-x"}

    asyncio.run(run())


def _skeleton_manifest() -> Manifest:
    """A manifest whose ONLY tool source is the operation projection: no builtin
    tool/agent/mcp modules named, ``api_tools`` enabled, and the real skeleton
    system-kinds router mounted so the projected op carries its route template."""
    return Manifest.model_validate(
        {
            "routers_modules": ["tai42_skeleton.routers.system_kinds"],
            "api_tools": {"enabled": True},
            # "none" keeps the surface to just the system-kinds router under test.
            "default_routers": "none",
        }
    )


def test_skeleton_operation_projects_at_boot():
    """A REAL skeleton leaf operation is projected as an MCP tool after start()."""

    async def run():
        async with app.app_context(_skeleton_manifest()):
            tools = await app.tools.get_tools()

            # The real skeleton leaf op is on the projected MCP surface.
            assert "list_system_kinds" in tools
            assert tools["list_system_kinds"].tags == {"system"}
            # A safe GET carries no destructive hint.
            assert getattr(tools["list_system_kinds"].annotations, "destructiveHint", None) in (None, False)
            # A destructive skeleton op projects WITH the destructive hint.
            set_annotations = tools["set_tool_extensions"].annotations
            assert set_annotations is not None
            assert set_annotations.destructiveHint is True

            # The system-kinds router re-attached its template + method to the SAME
            # registered record the projection read — proving the repopulate ran
            # before the routers, so the record the projection and authz hold is the
            # one the route decorates.
            md = operation_registry.get("list_system_kinds")
            assert md.route_template == "/api/system/kinds"
            assert md.http_method == "GET"

            # The tool-edge authorization governs the projected tool: the middleware
            # is installed and resolves the tool name back to its operation.
            assert any(isinstance(m, AuthzMiddleware) for m in app._fast_mcp.middleware)
            resolved = resolve_dispatch(
                "list_system_kinds",
                {},
                tool_registry=getattr(app, "_tool_registry", None),
                preset_manager=getattr(app, "preset_manager", None),
            )
            assert resolved is not None
            assert resolved.operation.name == "list_system_kinds"

    asyncio.run(run())


def test_skeleton_projection_survives_reload():
    """The skeleton op stays projected across a reload — the clear()+repopulate
    cycle re-registers it, so projection does not silently empty after a reload."""

    async def run():
        async with app.app_context(_skeleton_manifest()):
            assert "list_system_kinds" in await app.tools.get_tools()

            # A reload re-runs start() (clear + repopulate + projection) on a worker
            # thread through the gate exactly as production does.
            await reload_with(app, _skeleton_manifest())

            tools = await app.tools.get_tools()
            assert "list_system_kinds" in tools
            # The route template survived the reload on the re-registered record, so
            # the tool-edge authorization can still synthesize the concrete path.
            assert operation_registry.get("list_system_kinds").route_template == "/api/system/kinds"
            # The security middleware is not dropped by a reload cycle.
            assert any(isinstance(m, AuthzMiddleware) for m in app._fast_mcp.middleware)

    asyncio.run(run())


def _index_of(middleware, cls) -> int:
    for i, m in enumerate(middleware):
        if isinstance(m, cls):
            return i
    pytest.fail(f"{cls.__name__} middleware not found in the add-order")


def test_turn_budget_middleware_registered_after_authz_on_main_and_sub_mcp(monkeypatch):
    """The synchronous turn budget is armed at the MCP tool-call edge: its middleware is
    installed on the main server AND on every sub-MCP mount, always AFTER ``AuthzMiddleware``
    in the add-order. fastmcp runs ``reversed(self.middleware)``, so the later-added budget
    is the innermost — a denied call never opens a window."""
    from starlette.applications import Starlette

    import tai42_skeleton.app.sub_mcp_app as sub_mod
    from tai42_skeleton.app.sub_mcp_app import SubMcpAppRouter

    instances: list = []
    real_fastmcp = sub_mod.FastMCP

    class _RecordingFastMCP(real_fastmcp):
        def __init__(self, *a, **k) -> None:
            super().__init__(*a, **k)
            instances.append(self)

    monkeypatch.setattr(sub_mod, "FastMCP", _RecordingFastMCP)

    async def run():
        async with app.app_context(_manifest()):
            # Main server: both middlewares present, the budget added after authz.
            main = app._fast_mcp.middleware
            assert _index_of(main, TurnBudgetMiddleware) > _index_of(main, AuthzMiddleware)

            router = cast("SubMcpAppRouter", app.sub_app.mcp_sub_app_router)
            async with router.lifespan(cast("Starlette", None)):
                await router.register_sub_mcp_app("http_svc", ["sample_greet"], transport="http")
                assert await router._get_or_build_app("http_svc") is not None

    asyncio.run(run())

    # Every sub-MCP mount re-adds both middlewares (the main server's never reach a
    # sub-mount), the budget after authz there too.
    assert instances, "no sub-MCP FastMCP was built"
    for inst in instances:
        assert _index_of(inst.middleware, TurnBudgetMiddleware) > _index_of(inst.middleware, AuthzMiddleware)
