"""A loop marked closing refuses every new lease of a loop-bound resource on it, and only on it.

Each test runs on an ``asyncio.run`` loop of its own, so the mark ends with that loop. A lease held
from before the mark keeps working until it is released; a client whose creation ends after the mark
is closed, and its lease refused (or, for a caller cancelled meanwhile, its cancellation raised).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from tai42_kit.clients import PooledClient
from tai42_kit.clients.base import _loop_clients
from tai42_kit.loop_bound import LoopRegistryMap, ResourceRegistry, release_loop_bound_resources
from tai42_kit.loop_closing import mark_loop_closing

from .loop_bound._registries import closer, registry_with

_CLOSING = "the loop is closing, its loop-bound resources are being released"


def _client_cls(log: list[str]) -> type[PooledClient]:
    """A fresh pooled-client class (the pool is keyed by class), noting each close in ``log``."""

    class _ProbeClient(PooledClient):
        async def _create(self, **kwargs: object) -> object:
            return object()

        async def _close(self, client: object) -> None:
            log.append("client")

    return _ProbeClient


async def _pooled(cls: type[PooledClient]) -> None:
    async with cls().current(url="probe"):
        pass


def _mark_running_loop_closing() -> None:
    mark_loop_closing(asyncio.get_running_loop())


def _loop_has_pools() -> bool:
    return asyncio.get_running_loop() in _loop_clients


def test_once_the_loop_is_marked_closing_a_new_client_lease_is_refused_and_opens_nothing():
    log: list[str] = []
    cls = _client_cls(log)

    async def _scenario() -> None:
        _mark_running_loop_closing()
        with pytest.raises(RuntimeError, match=_CLOSING) as refused:
            await _pooled(cls)
        assert str(refused.value).startswith("_ProbeClient cannot lease a client on event loop ")
        assert _loop_has_pools() is False

    asyncio.run(_scenario())
    assert log == []


def test_once_the_loop_is_marked_closing_a_registry_and_a_get_from_one_already_held_are_refused():
    log: list[str] = []

    async def _scenario() -> None:
        rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
        registry = await registry_with(rmap, closer(log, "registry"))
        _mark_running_loop_closing()

        async def _factory() -> tuple[object, Callable[[], Awaitable[None]]]:
            return object(), closer(log, "reopened")

        with pytest.raises(RuntimeError, match=f"LoopRegistryMap cannot serve a registry on event loop .*{_CLOSING}"):
            rmap.current()
        with pytest.raises(RuntimeError, match=f"ResourceRegistry cannot serve a resource on event loop .*{_CLOSING}"):
            await registry._get_or_init_resource("other", _factory)
        await release_loop_bound_resources()

    asyncio.run(_scenario())
    assert log == ["registry"]


def test_a_lease_held_across_the_mark_is_not_refused_and_the_release_closes_its_client():
    log: list[str] = []
    cls = _client_cls(log)

    async def _scenario() -> None:
        async with cls().current(url="probe") as client:
            _mark_running_loop_closing()
            assert client is not None
        await release_loop_bound_resources()
        assert _loop_has_pools() is False

    asyncio.run(_scenario())
    assert log == ["client"]


def _slow_client_cls(created: asyncio.Event, finish: asyncio.Event, made: list[object], closed: list[object]):
    """A pooled-client class whose creation signals ``created`` and then waits for ``finish``."""

    class _SlowClient(PooledClient):
        async def _create(self, **kwargs: object) -> object:
            client = object()
            made.append(client)
            created.set()
            await finish.wait()
            return client

        async def _close(self, client: object) -> None:
            closed.append(client)

    return _SlowClient


def test_a_client_whose_creation_ends_after_the_mark_is_closed_and_the_lease_refused():
    made: list[object] = []
    closed: list[object] = []

    async def _scenario() -> None:
        created, finish = asyncio.Event(), asyncio.Event()
        lease = asyncio.create_task(_pooled(_slow_client_cls(created, finish, made, closed)))
        await created.wait()
        _mark_running_loop_closing()
        finish.set()
        with pytest.raises(RuntimeError, match=_CLOSING):
            await lease

    asyncio.run(_scenario())
    assert len(made) == 1
    assert closed == made


def test_a_caller_cancelled_once_its_client_is_created_after_the_mark_is_cancelled_and_the_client_closed():
    made: list[object] = []
    closed: list[object] = []

    async def _scenario() -> asyncio.Task[None]:
        created, finish = asyncio.Event(), asyncio.Event()
        lease = asyncio.create_task(_pooled(_slow_client_cls(created, finish, made, closed)))
        await created.wait()
        _mark_running_loop_closing()
        finish.set()
        # The creation finishes in the loop's next step; the caller is cancelled after it, before it
        # resumes, so the caller's wait ends with the cancellation instead of the client.
        await asyncio.sleep(0)
        lease.cancel()
        with pytest.raises(asyncio.CancelledError):
            await lease
        return lease

    lease = asyncio.run(_scenario())
    assert lease.cancelled() is True
    assert len(made) == 1
    assert closed == made


def test_a_closing_loop_leaves_every_other_loop_leasing():
    log: list[str] = []
    cls = _client_cls(log)

    async def _marked() -> None:
        _mark_running_loop_closing()
        with pytest.raises(RuntimeError, match=_CLOSING):
            await _pooled(cls)

    async def _lease_on_another_loop() -> bool:
        await _pooled(cls)
        has_pools = _loop_has_pools()
        await release_loop_bound_resources()
        return has_pools

    asyncio.run(_marked())
    assert asyncio.run(_lease_on_another_loop()) is True
    assert log == ["client"]
