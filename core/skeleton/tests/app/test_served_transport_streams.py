"""On every served MCP transport, the session's server-to-client stream is never counted against the drain budget.

The assembled app runs under uvicorn on a loopback port and the pinned SDK's own clients
hold a session open on each transport door: the main streamable-HTTP endpoint, a
streamable-HTTP sub-MCP mount, the main SSE transport and an SSE sub-MCP mount. The
session's stream (the streamable-HTTP standalone GET, the SSE session GET) never finishes
on its own, so while the session idles the generation counts nothing in flight, and a
reload's retire neither waits its budget on the stream nor reports it. A POST on the same
path stays counted. A reload driven by a call of the session records that call as its
driver, so the retire defers and the reload answers at once, on every door.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from tests.app import _reload_drive_state

from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app import epoch_retire as epoch_retire_mod
from tai42_skeleton.app.epoch import current_epoch
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.reload_gate import reload_gate

from ._served_app import (
    ADMIN_KEY,
    MCP_CALL_TIMEOUT_S,
    RELOAD_SWAP_BOUND_S,
    REQUEST_TIMEOUT_S,
    ResponseLog,
    bearer,
    install_access_control,
    logging_client_factory,
    served,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning"),
    # The SDK answers a streamable-HTTP POST with an SSE response whose reader stream the
    # SSE response iterates to its end and never closes; anyio reports it when freed.
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning"),
]

DRIVE_MODULE = "tests.app._fixtures.reload_drive"
MANIFEST = {
    "default_routers": "none",
    "tools": [{"title": "drive", "module": DRIVE_MODULE, "include": ["drive_reload", "held_call"]}],
}
TOOLS = ["drive_reload", "held_call"]
DRAIN_BUDGET_S = 2.0
DRAIN_ERROR = "still in flight after the drain budget"
# A held tool call answers only once the test releases it: after the in-flight settle (2), a
# second session's open (REQUEST_TIMEOUT_S), the reload's rebuild and swap (RELOAD_SWAP_BOUND_S)
# and the wait for the drain's report (DRAIN_BUDGET_S * 10).
HELD_CALL_TIMEOUT_S = 2 + REQUEST_TIMEOUT_S + RELOAD_SWAP_BOUND_S + DRAIN_BUDGET_S * 10
DOORS = ["streamable-main", "streamable-mount", "sse-main", "sse-mount"]


@pytest.fixture(autouse=True)
def _gate(monkeypatch: pytest.MonkeyPatch) -> None:
    install_access_control(monkeypatch)


@pytest.fixture(autouse=True)
def _process_state_restored(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restore what a boot leaves on the process: the env it applied and the registry's records."""
    from tai42_skeleton.app.route_registry import route_registry

    monkeypatch.setenv("TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS", str(DRAIN_BUDGET_S))
    snapshot = dict(os.environ)
    monkeypatch.setattr(route_registry, "_routes", dict(route_registry._routes))
    monkeypatch.setattr(route_registry, "_control_plane_mount", route_registry._control_plane_mount)
    monkeypatch.setattr(app.config.config_manager, "read_env", dict)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    epoch_mod._loaded_env_keys = set()


@asynccontextmanager
async def _door_url(monkeypatch: pytest.MonkeyPatch, door: str) -> AsyncIterator[str]:
    """Serve the manifest and yield the URL a client opens a session at on ``door``."""
    transport, _, where = door.partition("-")
    main_transport = "sse" if (transport, where) == ("sse", "main") else "http"
    async with served(monkeypatch, MANIFEST, transport=main_transport) as server:
        if where == "main":
            yield f"{server.base_url}/sse" if transport == "sse" else f"{server.base_url}/mcp"
            return
        slug = f"app-{transport}"
        await app.sub_app.mcp_sub_app_router.register_sub_mcp_app(
            slug, TOOLS, transport="sse" if transport == "sse" else "http"
        )
        yield f"{server.base_url}/app/{slug}/sse" if transport == "sse" else f"{server.base_url}/app/{slug}"


@asynccontextmanager
async def _session(url: str, *, call_timeout: float = MCP_CALL_TIMEOUT_S) -> AsyncIterator[ClientSession]:
    """An initialised SDK client session at ``url`` whose server-to-client stream the server has opened.

    ``call_timeout`` is the session's read timeout: a call unanswered by then fails with the
    SDK's named timeout error. A streamable-HTTP POST's HTTP read bound stays above it.
    """
    log = ResponseLog()
    factory = logging_client_factory(log)
    read_timeout = timedelta(seconds=call_timeout)
    if url.endswith("/sse"):
        async with (
            sse_client(url, headers=bearer(ADMIN_KEY), timeout=REQUEST_TIMEOUT_S, httpx_client_factory=factory) as (
                read,
                write,
            ),
            ClientSession(read, write, read_timeout_seconds=read_timeout) as session,
        ):
            await session.initialize()
            await session.list_tools()
            yield session
        return
    # The HTTP read bound stays above the session's: the SDK's streamable-HTTP client ends a
    # POST whose HTTP read times out without telling the caller, so that call would wait unbounded.
    client = factory(
        headers=bearer(ADMIN_KEY), timeout=httpx.Timeout(REQUEST_TIMEOUT_S, read=call_timeout + REQUEST_TIMEOUT_S)
    )
    async with (
        client,
        streamable_http_client(url, http_client=client) as (read, write, _session_id),
        ClientSession(read, write, read_timeout_seconds=read_timeout) as session,
    ):
        await session.initialize()
        await session.list_tools()
        # The SDK client opens its standalone GET stream after ``initialized``; wait until
        # the server has answered it (the stream is then admitted and open).
        async with asyncio.timeout(REQUEST_TIMEOUT_S):
            while not log.get_streams():
                await asyncio.sleep(0.01)
        yield session


@pytest.fixture
def retire_durations(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """How long each retire of a swapped-out generation ran inside its reload, in seconds.

    The retire is timed apart from the rebuild, whose duration depends on the host.
    """
    durations: list[float] = []
    retire = epoch_mod.retire_epoch

    async def _timed(*args: object, **kwargs: object) -> None:
        start = time.monotonic()
        try:
            await retire(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            durations.append(time.monotonic() - start)

    monkeypatch.setattr(epoch_mod, "retire_epoch", _timed)
    return durations


async def _reload_from_outside_any_session() -> None:
    """Run the real config reload through the gate from no request (a separate door)."""
    await reload_gate.run(lambda: app.admin.reload_config(), reimports=False)


async def _settled_in_flight() -> int:
    """The live generation's in-flight count once the requests a client already read have ended.

    A client reads a reply before the server's request handler has returned, so a count read
    at once can still include the request just answered.
    """
    epoch = current_epoch()
    deadline = time.monotonic() + 2.0
    while epoch.in_flight and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return epoch.in_flight


async def _deferred_tails_finished() -> None:
    async with asyncio.timeout(DRAIN_BUDGET_S * 10):
        await asyncio.gather(*epoch_retire_mod._deferred_retire_tasks)


async def _until_set_while_running(event: asyncio.Event, call: asyncio.Task[object], bound: float, what: str) -> None:
    """Wait for ``event`` while ``call`` runs: a call that ends first, or ``bound`` seconds passing, fails the test."""
    waiter = asyncio.ensure_future(event.wait())
    try:
        await asyncio.wait({waiter, call}, timeout=bound, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    if event.is_set():
        return
    if call.done():
        outcome = call.exception() if call.exception() is not None else call.result()
        pytest.fail(f"the call ended before {what}: {outcome!r}")
    pytest.fail(f"{what} did not happen within {bound} s")


def _drain_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if DRAIN_ERROR in record.getMessage()]


@pytest.mark.parametrize("door", DOORS)
async def test_an_idle_sessions_stream_is_not_in_flight_and_a_reload_does_not_wait_on_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, retire_durations: list[float], door: str
) -> None:
    async with _door_url(monkeypatch, door) as url, _session(url) as session:
        assert await _settled_in_flight() == 0, "the idle session's stream is counted against the drain"
        with caplog.at_level(logging.ERROR):
            await _reload_from_outside_any_session()
        [took] = retire_durations
        assert took < DRAIN_BUDGET_S, f"the retire waited {took:.2f}s on the session's stream"
        assert _drain_errors(caplog) == []
        if not door.startswith("sse"):
            # The retire terminated the streamable session; its next call reads the documented break.
            with pytest.raises(McpError, match="Session terminated"):
                await session.list_tools()


@pytest.mark.parametrize("door", DOORS)
async def test_a_reload_driven_by_a_session_call_answers_at_once_and_reports_no_straggler(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, retire_durations: list[float], door: str
) -> None:
    _reload_drive_state.reset()
    with caplog.at_level(logging.ERROR):
        async with _door_url(monkeypatch, door) as url:
            async with _session(url) as session:
                result = await session.call_tool("drive_reload", {})
                assert not result.isError, result
                observed = result.structuredContent
                assert observed is not None
                assert observed["engaged"] is True, f"the reload recorded no driver on {door}"
                [took] = retire_durations
                assert took < DRAIN_BUDGET_S, f"the retire drained for {took:.2f}s inside its driver on {door}"
                await _deferred_tails_finished()
            await asyncio.sleep(DRAIN_BUDGET_S)
    assert _drain_errors(caplog) == []


async def test_a_held_call_stays_counted_beside_the_exempt_stream_and_answers_before_the_close(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A tool call held past the drain budget is the one straggler the drain reports (never the
    stream), and its reply is delivered on its own POST while the driver keeps the transports open."""
    state = _reload_drive_state.reset(hold_driver=True)
    with caplog.at_level(logging.ERROR):
        async with (
            _door_url(monkeypatch, "streamable-main") as url,
            _session(url, call_timeout=HELD_CALL_TIMEOUT_S) as held_session,
        ):
            boot = current_epoch()
            held = asyncio.create_task(held_session.call_tool("held_call", {}))
            await _until_set_while_running(state.held_started, held, REQUEST_TIMEOUT_S, "the held call started")
            assert await _settled_in_flight() == 1, "only the held call's POST is in flight"
            async with _session(url, call_timeout=HELD_CALL_TIMEOUT_S) as driving_session:
                driving = asyncio.create_task(driving_session.call_tool("drive_reload", {}))
                await _until_set_while_running(
                    state.driver_swapped, driving, MCP_CALL_TIMEOUT_S, "the driven reload swapped"
                )
                # The retire's tail drains the held call for the budget, then reports it.
                async with asyncio.timeout(DRAIN_BUDGET_S * 10):
                    while not _drain_errors(caplog):
                        await asyncio.sleep(0.05)
                assert _drain_errors(caplog) == [f"epoch {boot.number} retire: 1 request(s) {DRAIN_ERROR}"]
                state.release_held.set()
                held_result = await held
                assert not held_result.isError, held_result
                assert held_result.structuredContent == {"result": "released"}
                state.release_driver.set()
                driving_result = await driving
                assert not driving_result.isError, driving_result
                await _deferred_tails_finished()
            assert boot.in_flight == 0
    assert len(_drain_errors(caplog)) == 1
