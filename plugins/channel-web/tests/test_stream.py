"""The SSE generator — transcript backlog, the ``chat.backlog_done`` marker, the
live tail, deadline keepalives, the loud stall on a black-holed XREAD, the pooled
connection's release before the first yield, and the concurrent-stream slot
accounting."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import reset_all_settings

from tai42_channel_web import store, stream
from tai42_channel_web.settings import WebSettings, web_settings
from tai42_channel_web.store import append_message
from tai42_channel_web.stream import StreamLimitError, check_stream_admission, stream_transcript

from .conftest import IDENTITY, VISITOR_ID, FakeRedis

pytestmark = pytest.mark.usefixtures("web_env")

_TRANSCRIPT_KEY = f"channel:web:transcript:{IDENTITY}:{VISITOR_ID}"


def _open_stream(request: Any, settings: WebSettings, address: str = VISITOR_ID):
    """The door's admission check, then the generator that takes and gives back the
    slot — the same order the door uses."""
    check_stream_admission(address, settings)
    return cast(
        AsyncGenerator[str],
        stream_transcript(cast(Request, request), IDENTITY, address, settings),
    )


@contextmanager
def _held_slots(*visitor_ids: str):
    """Hold live stream slots the way running generators do, so an admission check
    sees them."""
    with ExitStack() as stack:
        for visitor_id in visitor_ids:
            stack.enter_context(stream._stream_slot(visitor_id))
        yield


class _DisconnectedRequest:
    async def is_disconnected(self) -> bool:
        return True


class _AliveRequest:
    """Connected for exactly ``alive`` tail iterations, then disconnected."""

    def __init__(self, alive: int) -> None:
        self._alive = alive

    async def is_disconnected(self) -> bool:
        if self._alive > 0:
            self._alive -= 1
            return False
        return True


async def _collect(gen) -> list[str]:
    return [frame async for frame in gen]


async def test_backlog_then_backlog_done(fake_redis: FakeRedis):
    await append_message(IDENTITY, VISITOR_ID, "in", "one")
    await append_message(IDENTITY, VISITOR_ID, "out", "two")

    frames = await _collect(_open_stream(_DisconnectedRequest(), web_settings()))

    assert frames[0].startswith("event: chat.message\ndata: ")
    assert '"text": "one"' in frames[0]
    assert '"text": "two"' in frames[1]
    assert frames[2] == "event: chat.backlog_done\ndata: {}\n\n"


async def test_tail_forwards_new_entry(fake_redis: FakeRedis):
    gen = _open_stream(_AliveRequest(alive=1), web_settings())
    frames: list[str] = []
    async for frame in gen:
        frames.append(frame)
        if "backlog_done" in frame:
            # The entry lands AFTER cursor capture, so it can come only from the tail.
            await append_message(IDENTITY, VISITOR_ID, "out", "live")
    assert any('"text": "live"' in f for f in frames)


async def test_keepalive_then_disconnect(fake_redis: FakeRedis):
    settings = cast(
        WebSettings,
        SimpleNamespace(
            keepalive_seconds=0,
            blocking_grace_seconds=0.05,
            max_streams_per_visitor=4,
            max_streams_total=500,
            backlog_batch_entries=200,
        ),
    )
    frames = await _collect(_open_stream(_AliveRequest(alive=1), settings))
    assert frames == ["event: chat.backlog_done\ndata: {}\n\n", ": keepalive\n\n"]


async def test_tail_stall_raises_runtime_error(stub_app, monkeypatch: pytest.MonkeyPatch):
    class _StallRedis:
        async def xrevrange(self, key, count=None):
            return []

        async def xrange(self, key, min="-", max="+", count=None):
            return []

        async def xread(self, streams, block=None):
            await asyncio.Event().wait()  # pragma: no cover - cancelled by wait_for

    stub_app.clients.by_class[RedisClient] = _StallRedis()
    settings = cast(
        WebSettings,
        SimpleNamespace(
            keepalive_seconds=0,
            blocking_grace_seconds=0.05,
            max_streams_per_visitor=4,
            max_streams_total=500,
            backlog_batch_entries=200,
        ),
    )

    gen = _open_stream(_AliveRequest(alive=3), settings)
    assert "backlog_done" in await gen.__anext__()
    with pytest.raises(RuntimeError, match="XREAD"):
        await gen.__anext__()


async def test_a_long_backlog_is_replayed_a_page_at_a_time(fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch):
    # Every entry still reaches the visitor, in order — but read COUNT-bounded, so a
    # 500-stream process never holds 500 whole transcripts at once.
    monkeypatch.setenv("CHANNEL_WEB_BACKLOG_BATCH_ENTRIES", "2")
    reset_all_settings()
    for i in range(5):
        await append_message(IDENTITY, VISITOR_ID, "in", f"m{i}")

    frames = await _collect(_open_stream(_DisconnectedRequest(), web_settings()))

    assert [f'"text": "m{i}"' in frame for i, frame in enumerate(frames[:5])] == [True] * 5
    assert frames[5] == "event: chat.backlog_done\ndata: {}\n\n"


async def test_an_entry_written_during_the_replay_is_emitted_once(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # The tail resumes from the cursor captured BEFORE the backlog, so a backlog read
    # to ``+`` hands the visitor every entry written during a slow replay twice — once
    # from the backlog and again from the tail. The backlog is bounded by that same
    # cursor instead.
    await append_message(IDENTITY, VISITOR_ID, "in", "before")
    real_capture = store.capture_cursor

    async def _capture_then_write(redis: Any, identity: str, address: str) -> str:
        cursor = await real_capture(redis, identity, address)
        # An agent reply lands between the cursor capture and the backlog read.
        await append_message(identity, address, "out", "during the replay")
        return cursor

    monkeypatch.setattr(store, "capture_cursor", _capture_then_write)

    frames = await _collect(_open_stream(_AliveRequest(alive=1), web_settings()))

    assert sum('"text": "during the replay"' in frame for frame in frames) == 1
    # And it arrives after the marker, from the tail — never inside the backlog.
    assert frames.index("event: chat.backlog_done\ndata: {}\n\n") == 1


async def test_the_pooled_connection_is_released_before_every_yield(
    fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    # The invariant the module is built around: a slow SSE client suspends this
    # generator between frames, so no yield may sit inside the pooled connection's
    # ``async with``. Moving one inside passes every other test in this file while
    # pinning a shared-pool connection per open stream for the stream's whole life.
    held = 0
    real_ctx = store.pooled_redis_ctx

    @asynccontextmanager
    async def _tracked() -> AsyncIterator[Any]:
        nonlocal held
        async with real_ctx() as redis:
            held += 1
            try:
                yield redis
            finally:
                held -= 1

    monkeypatch.setattr(store, "pooled_redis_ctx", _tracked)
    monkeypatch.setenv("CHANNEL_WEB_BACKLOG_BATCH_ENTRIES", "1")
    reset_all_settings()
    for i in range(3):
        await append_message(IDENTITY, VISITOR_ID, "in", f"m{i}")

    frames: list[str] = []
    async for frame in _open_stream(_DisconnectedRequest(), web_settings()):
        assert held == 0, "a pooled store connection is held across an SSE yield"
        frames.append(frame)

    assert len(frames) == 4


# -- concurrent-stream slots ----------------------------------------------------


async def test_a_finished_stream_releases_its_slot(fake_redis: FakeRedis):
    await _collect(_open_stream(_DisconnectedRequest(), web_settings()))
    assert stream._open_streams == {}


async def test_a_raising_stream_still_releases_its_slot(stub_app):
    class _BoomRedis:
        async def xrevrange(self, key, count=None):
            raise RuntimeError("redis down")

    stub_app.clients.by_class[RedisClient] = _BoomRedis()
    with pytest.raises(RuntimeError, match="redis down"):
        await _collect(_open_stream(_DisconnectedRequest(), web_settings()))
    assert stream._open_streams == {}


async def test_a_stream_that_is_never_advanced_takes_no_slot(fake_redis: FakeRedis):
    # THE leak: the slot used to be taken at the door and given back only by the
    # generator's ``finally`` — and an async generator that is never advanced runs no
    # ``finally`` at all. Starlette awaits ``http.response.start`` before the first
    # ``__anext__``, and a disconnect there kills the response before the body ever
    # begins, so every aborted open would pin a slot for the life of the process.
    gen = _open_stream(_DisconnectedRequest(), web_settings())
    assert stream._open_streams == {}
    await gen.aclose()
    assert stream._open_streams == {}


async def test_a_stream_abandoned_after_one_frame_still_releases_its_slot(fake_redis: FakeRedis):
    await append_message(IDENTITY, VISITOR_ID, "in", "one")
    gen = _open_stream(_DisconnectedRequest(), web_settings())
    await gen.__anext__()
    assert stream._open_streams == {VISITOR_ID: 1}
    await gen.aclose()
    assert stream._open_streams == {}


def test_per_visitor_cap_refuses_the_next_stream(monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_MAX_STREAMS_PER_VISITOR", "2")
    reset_all_settings()
    settings = web_settings()
    with _held_slots(VISITOR_ID, VISITOR_ID):
        with pytest.raises(StreamLimitError, match="its cap of 2 open") as refused:
            check_stream_admission(VISITOR_ID, settings)
        assert "for this session" in refused.value.visitor_message
        # Another visitor still has their own allowance.
        check_stream_admission("other-visitor", settings)


def test_process_cap_refuses_a_stream_from_any_visitor(monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_MAX_STREAMS_TOTAL", "2")
    reset_all_settings()
    settings = web_settings()
    with _held_slots("visitor-a", "visitor-b"):
        with pytest.raises(StreamLimitError, match="cap of 2 concurrent") as refused:
            check_stream_admission("visitor-c", settings)
        assert "this server is carrying" in refused.value.visitor_message
