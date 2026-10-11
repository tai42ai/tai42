"""On a served SSE transport, a retiring generation ends the sessions it served, after their calls have answered.

The assembled app runs under uvicorn on a loopback port and the pinned SDK's own SSE client
holds a session on the main SSE transport and on an SSE sub-MCP mount. The legacy HTTP+SSE
transport has no session-termination message, so a reload ends each session the retired
generation served by ending its stream: a call still pending on the session is answered
with the client's ``Connection closed`` error instead of waiting for ever. A call of the
session is counted against the drain while it runs, so its reply is delivered before the
session ends; a reload driven by a call of the session answers on it before the end.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

import pytest
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.shared.exceptions import McpError

from tai42_skeleton.app.epoch import Epoch, current_epoch

from . import _reload_drive_state
from ._served_app import (
    ADMIN_KEY,
    MCP_CALL_TIMEOUT_S,
    RELOAD_SWAP_BOUND_S,
    REQUEST_TIMEOUT_S,
    ResponseLog,
    bearer,
    logging_client_factory,
)
from .test_served_transport_streams import (  # noqa: F401 - the autouse fixtures these cases need
    DRAIN_BUDGET_S,
    _deferred_tails_finished,
    _door_url,
    _drain_errors,
    _gate,
    _process_state_restored,
    _reload_from_outside_any_session,
    _settled_in_flight,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning"),
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning"),
]

SSE_DOORS = ["sse-main", "sse-mount"]
# Far below the client's own bounds: a session the retire leaves open never answers.
ANSWER_BOUND_S = 3.0
ASGI_ERRORS = ("Exception in ASGI application", "after response already completed")
# A drain budget no released call races: the held call answers within it however loaded the host.
UNRACED_DRAIN_BUDGET_S = 30.0


@dataclass
class SseSession:
    """An initialised SDK SSE session and every response its client saw."""

    session: ClientSession
    log: ResponseLog

    def refused_posts(self) -> int:
        return sum(1 for method, _path, status, _type in self.log.entries if method == "POST" and status == 404)


@asynccontextmanager
async def _sse_session(url: str) -> AsyncIterator[SseSession]:
    log = ResponseLog()
    async with (
        sse_client(
            url, headers=bearer(ADMIN_KEY), timeout=REQUEST_TIMEOUT_S, httpx_client_factory=logging_client_factory(log)
        ) as (read, write),
        # A call unanswered within the bound fails with the SDK's named timeout error.
        ClientSession(read, write, read_timeout_seconds=timedelta(seconds=MCP_CALL_TIMEOUT_S)) as session,
    ):
        await session.initialize()
        await session.list_tools()
        yield SseSession(session, log)


@pytest.fixture
def asgi_errors() -> Iterator[list[str]]:
    """Every ERROR the ASGI server logged while the test ran (its logger does not reach the root logger)."""
    records: list[str] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage() + (str(record.exc_info[1]) if record.exc_info else ""))

    handler = _Collect(level=logging.ERROR)
    server_logger = logging.getLogger("uvicorn.error")
    server_logger.addHandler(handler)
    try:
        yield records
    finally:
        server_logger.removeHandler(handler)


@pytest.fixture
def close_gate(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """Hold the boot generation's transport close until the returned event is set.

    Lets a test issue a call on a session between the swap and the close of its generation.
    """
    release = asyncio.Event()
    close = Epoch.close_transports
    boot: list[Epoch] = []

    async def _held(self: Epoch) -> None:
        if not boot:
            boot.append(self)
        if self is boot[0]:
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                await release.wait()
        await close(self)

    monkeypatch.setattr(Epoch, "close_transports", _held)
    return release


async def _swapped_from(boot: Epoch) -> None:
    async with asyncio.timeout(RELOAD_SWAP_BOUND_S):
        while current_epoch() is boot:
            await asyncio.sleep(0.01)


async def _refused_once(sse: SseSession) -> None:
    async with asyncio.timeout(REQUEST_TIMEOUT_S):
        while not sse.refused_posts():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("door", SSE_DOORS)
async def test_a_call_pending_on_a_session_of_the_retired_generation_reads_connection_closed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    asgi_errors: list[str],
    close_gate: asyncio.Event,
    door: str,
) -> None:
    """A reload from outside the session swaps the generation; the session's next call is
    refused by the new generation's transport (it has no such session) and waits on the
    session; the retired generation's close ends the session and the call reads the named
    error."""
    with caplog.at_level(logging.ERROR):
        async with _door_url(monkeypatch, door) as url, _sse_session(url) as sse:
            boot = current_epoch()
            reload = asyncio.create_task(_reload_from_outside_any_session())
            await _swapped_from(boot)
            pending = asyncio.create_task(sse.session.list_tools())
            await _refused_once(sse)
            close_gate.set()
            await reload
            with pytest.raises(McpError, match="Connection closed"):
                async with asyncio.timeout(ANSWER_BOUND_S):
                    await pending
            assert boot.in_flight == 0
    assert _drain_errors(caplog) == []
    assert [error for error in asgi_errors if any(text in error for text in ASGI_ERRORS)] == []


@pytest.mark.parametrize("door", SSE_DOORS)
async def test_a_reload_driven_on_a_session_answers_on_it_and_then_ends_it(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    asgi_errors: list[str],
    close_gate: asyncio.Event,
    door: str,
) -> None:
    """The driver's reply is delivered on the session; the session's next call waits until
    the retire's tail closes the generation, which ends the session after the driver answered."""
    _reload_drive_state.reset()
    with caplog.at_level(logging.ERROR):
        async with _door_url(monkeypatch, door) as url, _sse_session(url) as sse:
            boot = current_epoch()
            result = await sse.session.call_tool("drive_reload", {})
            assert not result.isError, result
            assert result.structuredContent is not None
            assert result.structuredContent["engaged"] is True
            pending = asyncio.create_task(sse.session.list_tools())
            await _refused_once(sse)
            close_gate.set()
            await _deferred_tails_finished()
            with pytest.raises(McpError, match="Connection closed"):
                async with asyncio.timeout(ANSWER_BOUND_S):
                    await pending
            assert boot.in_flight == 0
            assert not boot.driving
    assert _drain_errors(caplog) == []
    assert [error for error in asgi_errors if any(text in error for text in ASGI_ERRORS)] == []


@pytest.mark.parametrize("door", SSE_DOORS)
async def test_a_session_call_in_flight_is_drained_before_its_session_ends(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, asgi_errors: list[str], door: str
) -> None:
    """A call of the session held while a reload from another door retires the generation is
    counted in flight; released within the budget, its reply is delivered before the end."""
    monkeypatch.setenv("TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS", str(UNRACED_DRAIN_BUDGET_S))
    state = _reload_drive_state.reset()
    with caplog.at_level(logging.ERROR):
        async with _door_url(monkeypatch, door) as url, _sse_session(url) as sse:
            boot = current_epoch()
            held = asyncio.create_task(sse.session.call_tool("held_call", {}))
            await state.held_started.wait()
            assert await _settled_in_flight() == 1, "the session's call is not counted against the drain"
            reload = asyncio.create_task(_reload_from_outside_any_session())
            await _swapped_from(boot)
            await asyncio.sleep(DRAIN_BUDGET_S / 4)
            assert not reload.done(), "the retire did not drain the session's call"
            state.release_held.set()
            held_result = await held
            assert not held_result.isError, held_result
            assert held_result.structuredContent == {"result": "released"}
            await reload
            assert boot.in_flight == 0
    assert _drain_errors(caplog) == []
    assert [error for error in asgi_errors if any(text in error for text in ASGI_ERRORS)] == []


@pytest.mark.parametrize("door", SSE_DOORS)
async def test_a_session_call_outliving_the_budget_is_reported_and_its_client_reads_connection_closed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, door: str
) -> None:
    state = _reload_drive_state.reset()
    with caplog.at_level(logging.ERROR):
        async with _door_url(monkeypatch, door) as url, _sse_session(url) as sse:
            boot = current_epoch()
            held = asyncio.create_task(sse.session.call_tool("held_call", {}))
            await state.held_started.wait()
            await _reload_from_outside_any_session()
            assert _drain_errors(caplog) == [
                f"epoch {boot.number} retire: 1 request(s) still in flight after the drain budget"
            ]
            with pytest.raises(McpError, match="Connection closed"):
                async with asyncio.timeout(ANSWER_BOUND_S):
                    await held
