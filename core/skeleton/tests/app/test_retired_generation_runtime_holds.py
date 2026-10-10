"""A served worker keeps no retired serving generation reachable through what its requests started.

The assembled app runs under uvicorn on a loopback port, on its own thread and loop as a
served worker does (the streaming-response library keeps its shutdown watch per thread,
so a fresh thread starts without one). A request leaves something long-lived behind —
the first streaming response a worker serves, a pooled connection a request opens — and a
reload then retires the generation that served it. The retired generation's serving
surface (the object every request's ``scope["app"]`` points at) must be collected once
its bounded holds expire; the failure message names every referrer chain still holding it.
"""

from __future__ import annotations

import asyncio
import gc
import os
import sys
import threading
import types
import weakref
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import httpx
import pytest
import uvloop

from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.reload_gate import reload_gate

from ._fixtures.pooled_store_names import POOLED_STORE_PATH, POOLED_STORE_URL_ENV
from ._served_app import ADMIN_KEY, ResponseLog, bearer, install_access_control, manifest, served
from ._served_app import streamable_session_tools as open_streamable_session

pytestmark = [
    pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning"),
    # The SDK answers a streamable-HTTP POST with an SSE response whose reader stream the
    # response iterates to its end and never closes; anyio reports that reader as unclosed
    # when it is freed. A third-party resource report, matched narrowly.
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning"),
]

_POOLED_STORE_ROUTER = "tests.app._fixtures.pooled_store_route"

# The access-control cache TTL the served app runs with: a request's cache entries, and
# the timers that expire them, last this long.
_CACHE_TTL_SECONDS = 1
# How long a retired generation with only bounded holds may take to be collected: above
# the cache TTL, with room for a loaded host.
_RELEASE_DEADLINE_SECONDS = 15.0
# How long a reload waits for the generation's in-flight requests.
_DRAIN_DEADLINE_SECONDS = 5.0

_LOOPS = pytest.mark.parametrize(
    "loop_factory",
    [asyncio.new_event_loop, uvloop.new_event_loop],
    ids=["asyncio", "uvloop"],
)


@pytest.fixture(autouse=True)
def _served_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Access control over in-memory stores with a short cache TTL; the env and route records a boot leaves restored."""
    from tai42_kit.settings import reset_all_settings

    from tai42_skeleton.app.route_registry import route_registry

    install_access_control(monkeypatch)
    snapshot = dict(os.environ)
    monkeypatch.setenv("ACCESS_CONTROL_CACHE_TTL_SECONDS", str(_CACHE_TTL_SECONDS))
    monkeypatch.setattr(route_registry, "_routes", dict(route_registry._routes))
    monkeypatch.setattr(route_registry, "_control_plane_mount", route_registry._control_plane_mount)
    monkeypatch.setattr(app.config.config_manager, "read_env", dict)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    epoch_mod._loaded_env_keys = set()
    reset_all_settings()


def _serve_on_a_fresh_thread(
    scenario: Callable[[], Coroutine[Any, Any, None]], loop_factory: Callable[[], asyncio.AbstractEventLoop]
) -> None:
    """Run ``scenario`` on a new thread with its own event loop, re-raising what it raised."""
    raised: list[BaseException] = []

    def _run() -> None:
        try:
            asyncio.run(scenario(), loop_factory=loop_factory)
        except BaseException as exc:
            raised.append(exc)

    worker = threading.Thread(target=_run, name="served-worker")
    worker.start()
    worker.join()
    if raised:
        raise raised[0]


def _describe(obj: object) -> str:
    if isinstance(obj, types.FrameType):
        return f"frame {obj.f_code.co_qualname} ({obj.f_code.co_filename}:{obj.f_lineno})"
    if isinstance(obj, types.ModuleType):
        return f"module {obj.__name__}"
    if isinstance(obj, asyncio.Task):
        coro = obj.get_coro()
        code = getattr(coro, "cr_code", None)
        return f"Task {obj.get_name()} coro={getattr(code, 'co_qualname', coro)!r}"
    return f"{type(obj).__module__}.{type(obj).__qualname__}"


def _is_root(obj: object) -> bool:
    return isinstance(obj, (types.ModuleType, types.FrameType, asyncio.Task, asyncio.Handle))


def _referrer_chains(target: object, *, max_depth: int = 30, max_chains: int = 6) -> list[str]:
    """The referrer chains from ``target`` up to a root (a module, a frame, a task, a loop callback)."""
    own_frames = {id(frame) for frame in _frames_of_this_module()}
    seen = {id(target)}
    frontier: list[list[object]] = [[target]]
    chains: list[str] = []
    for _ in range(max_depth):
        deeper: list[list[object]] = []
        for path in frontier:
            referrers = gc.get_referrers(path[-1])
            for referrer in referrers:
                if id(referrer) in seen or id(referrer) in own_frames or referrer is frontier or referrer is deeper:
                    continue
                if isinstance(referrer, list) and any(referrer is p for p in frontier):
                    continue
                seen.add(id(referrer))
                extended = [*path, referrer]
                if _is_root(referrer):
                    chains.append(" <- ".join(_describe(obj) for obj in extended))
                    if len(chains) >= max_chains:
                        return chains
                else:
                    deeper.append(extended)
            del referrers
        frontier = deeper
    return chains


def _frames_of_this_module() -> list[types.FrameType]:
    frames = []
    frame: types.FrameType | None = sys._getframe()
    while frame is not None:
        if frame.f_code.co_filename == __file__:
            frames.append(frame)
        frame = frame.f_back
    return frames


def _chains_to(surface: weakref.ref[object]) -> str:
    alive = surface()
    return "collected" if alive is None else "\n".join(_referrer_chains(alive))


async def _assert_collected(surface: weakref.ref[object], held_by: str) -> None:
    """Wait for the retired generation's serving surface to be collected; fail naming what still holds it."""
    deadline = asyncio.get_running_loop().time() + _RELEASE_DEADLINE_SECONDS
    while True:
        gc.collect()
        if surface() is None:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"the retired generation that {held_by} is still reachable:\n{_chains_to(surface)}")
        await asyncio.sleep(0.1)


async def _reload() -> None:
    """Retire the serving generation through the reload door."""
    await reload_gate.run(app.admin.reload_config, reimports=True)


def _boot_surface() -> weakref.ref[object]:
    surface = epoch_mod.current_epoch().serving_surface()
    assert surface is not None
    return weakref.ref(surface)


@_LOOPS
def test_the_first_streaming_response_keeps_no_retired_generation_reachable(
    loop_factory: Callable[[], asyncio.AbstractEventLoop], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with served(monkeypatch, manifest("none")) as server:
            surface = _boot_surface()
            await open_streamable_session(f"{server.base_url}/mcp", bearer(ADMIN_KEY), ResponseLog())
            await _reload()
            await _assert_collected(surface, "served the worker's first streaming response")

    _serve_on_a_fresh_thread(scenario, loop_factory)


@pytest.mark.integration
@_LOOPS
def test_a_pooled_redis_connection_a_request_opened_keeps_no_retired_generation_reachable(
    loop_factory: Callable[[], asyncio.AbstractEventLoop], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = os.environ.get("TAI42_SKELETON_REAL_REDIS_URL")
    if not url:
        pytest.skip("real-Redis pooled-connection test is opt-in: set TAI42_SKELETON_REAL_REDIS_URL")
    monkeypatch.setenv(POOLED_STORE_URL_ENV, url)
    served_manifest = {**manifest("none"), "routers_modules": [_POOLED_STORE_ROUTER]}
    monkeypatch.setattr(app.config.config_manager, "read_manifest", lambda: served_manifest)

    async def scenario() -> None:
        async with served(monkeypatch, served_manifest) as server, httpx.AsyncClient() as client:
            surface = _boot_surface()
            request = asyncio.create_task(client.get(f"{server.base_url}{POOLED_STORE_PATH}", timeout=30))
            async with asyncio.timeout(10):
                while epoch_mod.current_epoch().in_flight == 0:
                    await asyncio.sleep(0.01)
            await _reload()
            response = await request
            assert response.status_code == 200, response.text
            del request, response
            await _assert_collected(surface, "admitted a request which opened a pooled Redis connection")

    _serve_on_a_fresh_thread(scenario, loop_factory)
