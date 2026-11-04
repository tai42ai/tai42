"""A release closes everything the kit keeps for the running loop: its loop-bound registries, then its pooled clients.

Each step runs whatever the other raised; one failure is raised as itself, several together. On a loop
that lives on, the next lease builds its pool or registry anew and the next release closes it.
"""

from __future__ import annotations

import asyncio

import pytest

from tai42_kit.clients import PooledClient
from tai42_kit.clients.base import _loop_clients
from tai42_kit.loop_bound import LoopRegistryMap, ResourceRegistry, release_loop_bound_resources

from ._registries import closer, registry_with


def _client_cls(log: list[str], *, close_fails: bool = False) -> type[PooledClient]:
    """A fresh pooled-client class (the pool is keyed by class), noting each close in ``log``."""

    class _ProbeClient(PooledClient):
        async def _create(self, **kwargs: object) -> object:
            return object()

        async def _close(self, client: object) -> None:
            log.append("client")
            if close_fails:
                raise RuntimeError("client close failed")

    return _ProbeClient


async def _pooled(cls: type[PooledClient]) -> None:
    async with cls().current(url="probe"):
        pass


def _loop_has_pools() -> bool:
    return asyncio.get_running_loop() in _loop_clients


async def test_the_registries_and_the_pooled_clients_are_released_registries_first():
    log: list[str] = []
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    registry = await registry_with(rmap, closer(log, "registry"))
    await _pooled(_client_cls(log))

    await release_loop_bound_resources()

    assert log == ["registry", "client"]
    assert registry.has_live_resources is False
    assert _loop_has_pools() is False


async def test_a_failing_registry_close_still_closes_the_pools_and_is_raised_as_itself():
    log: list[str] = []
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    await registry_with(rmap, closer(log, "registry", fails=True))
    await _pooled(_client_cls(log))

    with pytest.raises(ExceptionGroup, match="errors releasing the loop's loop-bound registries") as excinfo:
        await release_loop_bound_resources()

    assert log == ["registry", "client"]
    assert _loop_has_pools() is False
    [registry_failure] = excinfo.value.exceptions
    assert isinstance(registry_failure, ExceptionGroup)
    assert [str(e) for e in registry_failure.exceptions] == ["registry close failed"]


async def test_both_steps_failing_raise_one_group_carrying_both():
    log: list[str] = []
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    await registry_with(rmap, closer(log, "registry", fails=True))
    await _pooled(_client_cls(log, close_fails=True))

    with pytest.raises(ExceptionGroup, match="releasing the loop's kit resources failed") as excinfo:
        await release_loop_bound_resources()

    assert log == ["registry", "client"]
    registries, clients = excinfo.value.exceptions
    assert isinstance(registries, ExceptionGroup)
    assert "loop-bound registries" in str(registries)
    assert isinstance(clients, ExceptionGroup)
    assert "shutting down clients" in str(clients)


async def test_nothing_kept_for_the_loop_returns_without_touching_anything():
    LoopRegistryMap(ResourceRegistry)
    await release_loop_bound_resources()
    assert _loop_has_pools() is False


async def test_a_loop_that_lives_on_leases_anew_after_a_release_and_the_next_release_closes_it():
    log: list[str] = []
    created: list[object] = []

    class _CountingClient(PooledClient):
        async def _create(self, **kwargs: object) -> object:
            client = object()
            created.append(client)
            return client

        async def _close(self, client: object) -> None:
            log.append("client")

    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    for generation in ("first", "second"):
        await registry_with(rmap, closer(log, f"{generation} registry"))
        await _pooled(_CountingClient)
        await release_loop_bound_resources()

    assert len(created) == 2
    assert log == ["first registry", "client", "second registry", "client"]
    assert _loop_has_pools() is False
