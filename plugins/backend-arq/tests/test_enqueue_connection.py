"""The arq enqueue connection rides the kit's pooled Redis client across requests and reloads.

An enqueue a request makes opens its connection in the kit's pool, which outlives the
request; the pooled connection keeps no context of that request. A reload that re-imports
the plugin leases the same pooled client again instead of opening a second pool. The Redis
server is an in-process fake served over TCP, so connections open real transports. The
context case runs on asyncio and, where it exists, uvloop.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import importlib
import sys
import threading
import weakref
from collections.abc import Callable, Iterator

import pytest
from fakeredis import TcpFakeServer
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.settings import cache_registry

import tai42_backend_arq
from tai42_backend_arq import pool
from tai42_backend_arq import settings as settings_module

_request: contextvars.ContextVar[object] = contextvars.ContextVar("request")

_LOOP_FACTORIES: list[tuple[str, Callable[[], asyncio.AbstractEventLoop]]] = [("asyncio", asyncio.new_event_loop)]
if sys.platform != "win32":
    import uvloop

    _LOOP_FACTORIES.append(("uvloop", uvloop.new_event_loop))


class _Marker:
    """An object only the enqueuing request's context holds."""


@pytest.fixture
def redis_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Serve an in-process fake Redis over TCP and point the backend's settings at it."""
    server = TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    url = f"redis://{host}:{port}/0"
    monkeypatch.setenv("ARQ_REDIS_URL", url)
    settings_module.arq_settings.cache_clear()
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        settings_module.arq_settings.cache_clear()


async def _marker_alive_after_an_enqueue() -> bool:
    async def request() -> weakref.ref[_Marker]:
        marker = _Marker()
        _request.set(marker)
        async with pool.arq_connection() as arq_redis:
            assert await arq_redis.enqueue_job("tool_execution") is not None
        return weakref.ref(marker)

    try:
        marker = await asyncio.create_task(request())
        await asyncio.sleep(0.1)
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()
        return marker() is not None
    finally:
        await shutdown_all_clients()


@pytest.mark.usefixtures("redis_url")
@pytest.mark.parametrize("loop_factory", [f for _, f in _LOOP_FACTORIES], ids=[n for n, _ in _LOOP_FACTORIES])
def test_an_enqueue_connection_keeps_no_context_of_the_request_that_opened_it(
    loop_factory: Callable[[], asyncio.AbstractEventLoop],
) -> None:
    assert asyncio.run(_marker_alive_after_an_enqueue(), loop_factory=loop_factory) is False


@pytest.mark.usefixtures("redis_url")
async def test_a_reload_re_import_leases_the_same_pooled_client(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        async with pool.arq_connection() as boot:
            boot_pool = boot.connection_pool
        # The re-import replaces the modules in ``sys.modules`` and on the package; both are
        # restored after the test, so the rest of the suite keeps the modules it imported.
        for name in ("pool", "settings"):
            monkeypatch.delitem(sys.modules, f"tai42_backend_arq.{name}")
            monkeypatch.setattr(tai42_backend_arq, name, getattr(tai42_backend_arq, name))
        reloaded = importlib.import_module("tai42_backend_arq.pool")
        async with reloaded.arq_connection() as after_reload:
            assert after_reload.connection_pool is boot_pool
    finally:
        # The re-import re-bound the one settings accessor to the re-imported module's class;
        # bind it back to the module the rest of the suite holds.
        cache_registry._CACHE_CLEARS[f"{settings_module.__name__}.arq_settings"].rebind(settings_module.ArqSettings)
        await shutdown_all_clients()
