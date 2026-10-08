"""The sub-MCP write service: the store-write-FIRST durability contract, up-front
validation (invalid input never reaches the store), the store+local unregister, and the
mount-change listeners notified after every durable mount change.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app

from tai42_skeleton.sub_mcp import service
from tai42_skeleton.sub_mcp import store as store_mod
from tai42_skeleton.sub_mcp.store import InMemorySubMcpStore


class _FakeRouter:
    def __init__(self, routes=None, register_error: Exception | None = None):
        self.routes = dict(routes or {})
        self.registered: list[tuple] = []
        self.unregistered: list[str] = []
        self._register_error = register_error

    async def register_sub_mcp_app(self, slug, tools, transport="http"):
        if self._register_error is not None:
            raise self._register_error
        self.registered.append((slug, tools, transport))
        from tai42_contract.sub_mcp import RouteConfig

        self.routes[slug] = RouteConfig(tools=tools, transport=transport)

    async def unregister_sub_mcp_app(self, slug):
        self.unregistered.append(slug)
        self.routes.pop(slug, None)


@pytest.fixture(autouse=True)
def _fresh_listeners(monkeypatch) -> None:
    """Each test starts with no mount-change listener registered."""
    monkeypatch.setattr(service, "_mount_change_listeners", [])


class _CountingListener:
    """A synthetic mount-change listener counting its notifications."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self._error = error

    async def __call__(self) -> None:
        self.calls += 1
        if self._error is not None:
            raise self._error


@pytest.fixture
def wired(monkeypatch):
    """Install a fresh in-memory store + a fake router behind ``tai42_app``."""

    def _wire(router: _FakeRouter) -> InMemorySubMcpStore:
        fresh = InMemorySubMcpStore()
        monkeypatch.setattr(store_mod, "_IN_MEMORY_STORE", fresh)
        monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(sub_app=SimpleNamespace(mcp_sub_app_router=router)))
        return fresh

    return _wire


async def test_register_writes_store_before_router(wired):
    router = _FakeRouter()
    store = wired(router)
    await service.register_sub_mcp_app("weather", ["get_forecast"], transport="sse")
    # Both halves landed, store first.
    assert (await store.get_route("weather")).tools == ["get_forecast"]
    assert router.registered == [("weather", ["get_forecast"], "sse")]


async def test_router_swap_failure_leaves_registration_recoverable(wired):
    # A crash in the in-process router swap AFTER the store write must leave the
    # registration DURABLE — the store-write-FIRST contract — so the next rehydrate
    # re-materializes it rather than losing it forever.
    router = _FakeRouter(register_error=RuntimeError("router boom"))
    store = wired(router)
    with pytest.raises(RuntimeError, match="router boom"):
        await service.register_sub_mcp_app("weather", ["get_forecast"])
    # The store write happened before the failing swap, so the registration survives.
    assert (await store.get_route("weather")).tools == ["get_forecast"]


async def test_invalid_slug_never_reaches_the_store(wired):
    # Validation runs BEFORE the store write, so a malformed slug raises without
    # persisting a garbage entry (and without touching the router).
    router = _FakeRouter()
    store = wired(router)
    with pytest.raises(ValueError, match="must match"):
        await service.register_sub_mcp_app("bad/slug", ["get_forecast"])
    assert await store.list_routes() == {}
    assert router.registered == []


async def test_invalid_transport_never_reaches_the_store(wired):
    router = _FakeRouter()
    store = wired(router)
    with pytest.raises(ValueError, match="transport"):
        await service.register_sub_mcp_app("weather", ["get_forecast"], transport="carrier-pigeon")
    assert await store.list_routes() == {}


async def test_unregister_removes_from_store_and_router(wired):
    from tai42_contract.sub_mcp import RouteConfig

    router = _FakeRouter(routes={"weather": RouteConfig(tools=["get_forecast"])})
    store = wired(router)
    await store.save_route("weather", RouteConfig(tools=["get_forecast"]))
    removed = await service.unregister_sub_mcp_app("weather")
    assert removed is True
    assert await store.get_route("weather") is None
    assert router.unregistered == ["weather"]


async def test_unregister_store_only_slug_returns_true(wired):
    from tai42_contract.sub_mcp import RouteConfig

    router = _FakeRouter()
    store = wired(router)
    await store.save_route("remote", RouteConfig(tools=["get_forecast"]))
    removed = await service.unregister_sub_mcp_app("remote")
    assert removed is True
    assert await store.get_route("remote") is None
    # Nothing was bound here, so the local router was left alone.
    assert router.unregistered == []


async def test_unregister_absent_slug_returns_false(wired):
    router = _FakeRouter()
    wired(router)
    assert await service.unregister_sub_mcp_app("ghost") is False
    assert router.unregistered == []


# -- mount-change listeners ----------------------------------------------------


async def test_register_notifies_the_listener_once_after_the_store_write(wired):
    router = _FakeRouter()
    store = wired(router)
    seen: list[bool] = []

    async def _listener() -> None:
        # The durable write has landed by the time a listener runs.
        seen.append(await store.get_route("weather") is not None)

    service.on_mount_changed(_listener)
    await service.register_sub_mcp_app("weather", ["get_forecast"])
    assert seen == [True]


async def test_real_unregister_notifies_the_listener_once(wired):
    from tai42_contract.sub_mcp import RouteConfig

    router = _FakeRouter(routes={"weather": RouteConfig(tools=["get_forecast"])})
    store = wired(router)
    await store.save_route("weather", RouteConfig(tools=["get_forecast"]))
    listener = _CountingListener()
    service.on_mount_changed(listener)
    assert await service.unregister_sub_mcp_app("weather") is True
    assert listener.calls == 1


async def test_store_only_unregister_notifies_the_listener_once(wired):
    from tai42_contract.sub_mcp import RouteConfig

    router = _FakeRouter()
    store = wired(router)
    await store.save_route("remote", RouteConfig(tools=["get_forecast"]))
    listener = _CountingListener()
    service.on_mount_changed(listener)
    assert await service.unregister_sub_mcp_app("remote") is True
    assert listener.calls == 1


async def test_no_op_unregister_notifies_nothing(wired):
    wired(_FakeRouter())
    listener = _CountingListener()
    service.on_mount_changed(listener)
    assert await service.unregister_sub_mcp_app("ghost") is False
    assert listener.calls == 0


async def test_router_swap_failure_still_notifies_then_raises(wired):
    # The store write is durable before the swap fails, so the change it made is still
    # announced; the swap's own error then propagates.
    router = _FakeRouter(register_error=RuntimeError("router boom"))
    wired(router)
    listener = _CountingListener()
    service.on_mount_changed(listener)
    with pytest.raises(RuntimeError, match="router boom"):
        await service.register_sub_mcp_app("weather", ["get_forecast"])
    assert listener.calls == 1


async def test_invalid_registration_notifies_nothing(wired):
    wired(_FakeRouter())
    listener = _CountingListener()
    service.on_mount_changed(listener)
    with pytest.raises(ValueError, match="must match"):
        await service.register_sub_mcp_app("bad/slug", ["get_forecast"])
    assert listener.calls == 0


async def test_registering_the_same_listener_twice_notifies_it_once(wired):
    wired(_FakeRouter())
    listener = _CountingListener()
    service.on_mount_changed(listener)
    service.on_mount_changed(listener)
    await service.register_sub_mcp_app("weather", ["get_forecast"])
    assert listener.calls == 1


async def test_listeners_run_in_registration_order(wired):
    wired(_FakeRouter())
    order: list[str] = []

    async def _first() -> None:
        order.append("first")

    async def _second() -> None:
        order.append("second")

    service.on_mount_changed(_first)
    service.on_mount_changed(_second)
    await service.register_sub_mcp_app("weather", ["get_forecast"])
    assert order == ["first", "second"]


async def test_a_listener_error_propagates_after_the_durable_write(wired):
    router = _FakeRouter()
    store = wired(router)
    service.on_mount_changed(_CountingListener(error=RuntimeError("invalidation down")))
    with pytest.raises(RuntimeError, match="invalidation down"):
        await service.register_sub_mcp_app("weather", ["get_forecast"])
    # The write the listener follows is durable; only the announcement failed, loudly.
    assert (await store.get_route("weather")).tools == ["get_forecast"]
