"""The interactions inbox SSE stream holds no settings instance while it tails.

The stream outlives a settings reload (it is exempt from the retire drain and runs
until the client disconnects), so it reads its configuration through the cached
accessor at the point of use. The case opens the stream through its route with the
REAL cached (epoch-stamped) accessor, parks the tail in its XREAD, retires the
settings generation the way the reload's retire step does (advance the epoch, reset,
sweep), then lets the tail end.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from starlette.requests import Request
from tai42_kit.clients.base import advance_client_epoch
from tai42_kit.settings import reset_all_settings, sweep_stale_settings

from tai42_skeleton.interactions.settings import interactions_settings
from tai42_skeleton.routers import interactions as router
from tai42_skeleton.routers.interactions.stream import _CONNECT_FRAME, stream


class _TailRedis:
    """A redis whose tail XREAD parks on ``gate`` and then returns no events."""

    def __init__(self, gate: asyncio.Future[None]) -> None:
        self._gate = gate
        self.xreads = 0

    async def xrevrange(self, key: str, count: int = 1) -> list[Any]:
        return []

    async def xread(self, streams: dict[str, str], block: int | None = None) -> list[Any]:
        self.xreads += 1
        await self._gate
        return []


class _OpenRequest:
    """A stream request that stays connected for one tail window, then disconnects."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.query_params: dict[str, str] = {}
        self.windows = 0

    async def is_disconnected(self) -> bool:
        self.windows += 1
        return self.windows > 1


async def test_an_open_inbox_stream_holds_no_retired_settings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    interactions_settings.cache_clear()
    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    redis = _TailRedis(gate)

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield redis

    monkeypatch.setattr(router, "client_ctx", _ctx)
    response = await stream(cast(Request, _OpenRequest()))
    body = response.body_iterator  # type: ignore[attr-defined]
    assert await anext(body) == _CONNECT_FRAME
    tail = asyncio.ensure_future(anext(body, None))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert redis.xreads == 1
    try:
        retired = advance_client_epoch()
        reset_all_settings()
        with caplog.at_level(logging.ERROR, logger="tai42_kit.settings.cache_registry"):
            held = [h for h in sweep_stale_settings(retired) if h.settings_type.endswith(".InteractionsSettings")]
    finally:
        gate.set_result(None)
        await tail
    assert held == []
    assert "InteractionsSettings" not in caplog.text


class _GenerationSurface:
    """A generation's serving surface: it keeps the settings its collaborators read at build."""

    def __init__(self, settings: object) -> None:
        self.settings = settings

    async def __call__(self, scope, receive, send) -> None:  # pragma: no cover
        raise AssertionError("the surface stand-in is never dispatched")


class _OpenRequestOn(_OpenRequest):
    """A stream request served by ``surface``: its scope points at the generation that serves it."""

    def __init__(self, surface: object) -> None:
        super().__init__()
        self.scope = {"app": surface}


@pytest.fixture
def _serving_generation() -> Iterator[None]:
    """A boot generation installed with no dispatch slot; the process serving state dropped after."""
    from tai42_kit.clients.base import current_client_epoch

    from tai42_skeleton.app import epoch as epoch_mod
    from tai42_skeleton.app.retired_generations import detach_retired_generations

    epoch_mod._current = epoch_mod.Epoch(number=current_client_epoch())
    try:
        yield
    finally:
        epoch_mod._current = None
        detach_retired_generations()


@pytest.mark.usefixtures("_serving_generation")
async def test_an_inbox_stream_open_across_a_reload_leaves_nothing_reported_while_open_or_after_it_closes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import gc

    from tai42_skeleton.app.epoch import Epoch, build_and_swap_epoch, current_epoch

    monkeypatch.setenv("INTERACTIONS_REDIS_URL", "redis://localhost:6379/0")
    env = {"INTERACTIONS_REDIS_URL": "redis://localhost:6379/0"}

    async def _build(_epoch: Epoch) -> _GenerationSurface:
        return _GenerationSurface(interactions_settings())

    async def _reload() -> None:
        await build_and_swap_epoch(env, rebuild=lambda: None, build_serving_app=_build, drain_deadline=0.1)

    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    redis = _TailRedis(gate)

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield redis

    monkeypatch.setattr(router, "client_ctx", _ctx)
    with caplog.at_level(logging.ERROR, logger="tai42_kit.settings.cache_registry"):
        await _reload()
        response = await stream(cast(Request, _OpenRequestOn(current_epoch().serving_app)))
        body = response.body_iterator  # type: ignore[attr-defined]
        assert await anext(body) == _CONNECT_FRAME
        tail = asyncio.ensure_future(anext(body, None))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert redis.xreads == 1
        try:
            await _reload()
            await asyncio.sleep(0)
            # The generation serving the open stream stays reachable by design: nothing reported.
            assert "Stale settings instance" not in caplog.text
        finally:
            gate.set_result(None)
            await tail
        await body.aclose()
        del response, body, tail
        gc.collect()
        await asyncio.sleep(0)
    # The client is gone, the generation is released and certified with nothing left of it.
    assert "Stale settings instance" not in caplog.text
