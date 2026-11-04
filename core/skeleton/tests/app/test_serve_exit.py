"""A serve worker's graceful stop ends its event streams only after its in-flight replies are written.

The served test runs the assembled app under uvicorn on a loopback port with the serve exit
armed and delivers SIGTERM to the process while an MCP ``tools/call`` is in flight: the
pinned SDK's own client receives the call's reply, and the server then ends its streams and
stops well inside uvicorn's graceful-shutdown cap. The arming tests drive the signal chain
against a ``uvicorn.Server`` handler installed by the test.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import TextContent
from sse_starlette.sse import AppStatus
from uvicorn.server import HANDLED_SIGNALS

from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app import serve_exit
from tai42_skeleton.app.epoch import Epoch
from tai42_skeleton.app.lifecycle.exit_drain import ExitDrain
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.app.serve_exit import arm_serve_exit

from ._served_app import ADMIN_KEY, INITIALIZE_BODY, REQUEST_TIMEOUT_S, bearer, install_access_control, served

pytestmark = [
    pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning"),
    # The SDK answers a streamable-HTTP POST with an SSE response whose reader stream the
    # SSE response iterates to its end and never closes; anyio reports it when freed.
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning"),
]

HELD_MODULE = "tests.app._fixtures.held_tools"
HELD_MANIFEST = {
    "default_routers": "none",
    "tools": [{"title": "held", "module": HELD_MODULE, "include": ["held_reply"]}],
}
SSE_MANIFEST = {
    "default_routers": "none",
    "tools": [{"title": "held", "module": HELD_MODULE, "include": ["held_reply", "sized_reply"]}],
}
# A reply far larger than the loopback socket buffers between the server and a client that
# does not read hold, so the session stream's write of it waits on the client's reads.
LARGE_REPLY_CHARS = 16 * 1024 * 1024
# Time for a call that answers to hand its reply to the session stream.
HAND_OFF_S = 0.3
# The response library's stream watch polls its shutdown flag every 0.5 s; the call is held
# in flight past that poll after the signal, so a stream the library ends never carries it.
HOLD_AFTER_SIGNAL_S = 1.0
# The client session's read timeout: a reply that never comes fails the call with the SDK's
# named timeout error.
CALL_READ_TIMEOUT_S = 5.0
# uvicorn's graceful-shutdown cap for the served test, far above the time the stop takes, so
# a stop that returns inside REQUEST_TIMEOUT_S needed no cancel by the cap.
SHUTDOWN_CAP_S = 60
# Loop turns that let every task a drain case started run until it suspends on an event.
LOOP_TURNS = 20


async def _turn_loop() -> None:
    for _ in range(LOOP_TURNS):
        await asyncio.sleep(0)


class _StopsBegan(list[str]):
    """A ``stop_began`` that records each reason it is called with."""

    def __call__(self, reason: str) -> bool:
        self.append(reason)
        return True


@pytest.fixture(autouse=True)
def _library_drain_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with the response library's automatic drain on and gets it back on afterwards."""
    monkeypatch.setattr(AppStatus, "enable_automatic_graceful_drain", True)
    monkeypatch.setattr(AppStatus, "should_exit", False)


@pytest.fixture
def signal_handlers_restored() -> Iterator[None]:
    """Restore the process's handlers for every signal uvicorn captures."""
    saved = {sig: signal.getsignal(sig) for sig in HANDLED_SIGNALS}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


@pytest.fixture
def sigterms_after_serve(signal_handlers_restored: None) -> list[int]:
    """The SIGTERM handler under the served server: uvicorn restores it and re-raises a captured signal at serve end."""
    received: list[int] = []
    signal.signal(signal.SIGTERM, lambda signum, _frame: received.append(signum))
    return received


async def test_an_in_flight_mcp_reply_is_delivered_when_the_graceful_stop_begins_during_the_call(
    monkeypatch: pytest.MonkeyPatch, sigterms_after_serve: list[int]
) -> None:
    install_access_control(monkeypatch)
    async with served(monkeypatch, HELD_MANIFEST, timeout_graceful_shutdown=SHUTDOWN_CAP_S) as running:
        arm_serve_exit(ExitDrain(), _StopsBegan())
        held = sys.modules[HELD_MODULE]
        client = create_mcp_http_client(headers=bearer(ADMIN_KEY), timeout=httpx.Timeout(REQUEST_TIMEOUT_S))
        async with (
            client,
            streamable_http_client(f"{running.base_url}/mcp", http_client=client) as (read, write, _session_id),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=CALL_READ_TIMEOUT_S)) as session,
        ):
            await session.initialize()
            # The SDK validates a call's result against the tool's listed output schema, listing
            # the tools first when it has not; listed now, the call sends no request after the
            # stop began, once the server has closed its listeners.
            await session.list_tools()
            call = asyncio.create_task(session.call_tool("held_reply", {"text": "answered"}))
            await asyncio.wait_for(held.ENTERED.wait(), REQUEST_TIMEOUT_S)
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(HOLD_AFTER_SIGNAL_S)
            held.RELEASE.set()
            result = await call
        assert not result.isError, result
        assert result.content == [TextContent(type="text", text="answered")]
        # The server ends its streams and stops by itself, without the harness's own exit.
        await asyncio.wait_for(asyncio.shield(running.serving), REQUEST_TIMEOUT_S)
    assert sigterms_after_serve == [signal.SIGTERM]


async def test_a_request_accepted_before_uvicorn_stops_accepting_is_answered(
    monkeypatch: pytest.MonkeyPatch, sigterms_after_serve: list[int]
) -> None:
    # uvicorn's handler only flags the stop; the server closes its listeners on its main loop's
    # next tick. The tick is held here, so the signal is followed by a session opened on a
    # listener that still accepts, as a client reconnecting at that moment does.
    install_access_control(monkeypatch)
    async with served(monkeypatch, HELD_MANIFEST, timeout_graceful_shutdown=SHUTDOWN_CAP_S) as running:
        arm_serve_exit(ExitDrain(), _StopsBegan())
        tick = running.server.on_tick
        accepting = asyncio.Event()

        async def held_tick(counter: int) -> bool:
            if running.server.should_exit:
                await accepting.wait()
            return await tick(counter)

        monkeypatch.setattr(running.server, "on_tick", held_tick)
        signal.raise_signal(signal.SIGTERM)
        client = create_mcp_http_client(headers=bearer(ADMIN_KEY), timeout=httpx.Timeout(REQUEST_TIMEOUT_S))
        try:
            async with (
                client,
                streamable_http_client(f"{running.base_url}/mcp", http_client=client) as (read, write, _session_id),
                ClientSession(read, write, read_timeout_seconds=timedelta(seconds=CALL_READ_TIMEOUT_S)) as session,
            ):
                initialized = await session.initialize()
        finally:
            accepting.set()
        assert initialized.serverInfo.name
        await asyncio.wait_for(asyncio.shield(running.serving), REQUEST_TIMEOUT_S)
    assert sigterms_after_serve == [signal.SIGTERM]


async def _sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    """The ``(event, data)`` pairs of a decoded SSE line stream; comments and other fields are skipped."""
    event, data = "message", []
    async for line in lines:
        if not line:
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)


async def test_an_sse_reply_handed_to_its_stream_before_the_graceful_stop_is_written_before_the_stream_ends(
    monkeypatch: pytest.MonkeyPatch, sigterms_after_serve: list[int]
) -> None:
    # The client stops reading the session stream after its calls. A large reply fills the
    # socket buffers, so the stream's write of it waits on the client's reads, and the held
    # call's reply, handed to the stream after it, waits behind that write. The stop begins
    # after both hand-offs, and the client reads again only once the response library's
    # stream watch has polled its flag past the signal.
    install_access_control(monkeypatch)
    async with served(monkeypatch, SSE_MANIFEST, transport="sse", timeout_graceful_shutdown=SHUTDOWN_CAP_S) as running:
        arm_serve_exit(ExitDrain(), _StopsBegan())
        held = sys.modules[HELD_MODULE]
        client = httpx.AsyncClient(headers=bearer(ADMIN_KEY), timeout=httpx.Timeout(REQUEST_TIMEOUT_S))
        async with client, client.stream("GET", f"{running.base_url}/sse") as stream:
            events = _sse_events(stream.aiter_lines())
            kind, endpoint = await anext(events)
            assert kind == "endpoint"

            async def post(message: dict[str, object]) -> None:
                answer = await client.post(f"{running.origin}{endpoint}", json=message)
                answer.raise_for_status()

            await post(json.loads(INITIALIZE_BODY))
            kind, data = await anext(events)
            assert (kind, json.loads(data)["id"]) == ("message", 1)
            await post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            await post(_tool_call(2, "held_reply", {"text": "answered"}))
            await asyncio.wait_for(held.ENTERED.wait(), REQUEST_TIMEOUT_S)
            await post(_tool_call(3, "sized_reply", {"size": LARGE_REPLY_CHARS}))
            await asyncio.sleep(HAND_OFF_S)
            held.RELEASE.set()
            await asyncio.sleep(HAND_OFF_S)
            signal.raise_signal(signal.SIGTERM)
            await asyncio.sleep(HOLD_AFTER_SIGNAL_S)
            replies: dict[object, dict[str, object]] = {}
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                async for kind, data in events:
                    if kind == "message":
                        reply = json.loads(data)
                        replies[reply["id"]] = reply["result"]
                    if len(replies) == 2:
                        break
        assert replies[3]["content"] == [{"type": "text", "text": "x" * LARGE_REPLY_CHARS}]
        assert replies[2]["content"] == [{"type": "text", "text": "answered"}]
        await asyncio.wait_for(asyncio.shield(running.serving), REQUEST_TIMEOUT_S)
    assert sigterms_after_serve == [signal.SIGTERM]


def _tool_call(request_id: int, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


async def _unserved_app(scope: object, receive: object, send: object) -> None:
    """The app of a server that is never started."""


class _RecordingServer(uvicorn.Server):
    """A uvicorn server whose signal handler records each signal it receives."""

    def __init__(self) -> None:
        super().__init__(uvicorn.Config(app=_unserved_app))
        self.exits: list[int] = []

    def handle_exit(self, sig: int, frame: object) -> None:
        self.exits.append(sig)


def _started_server(*listeners: asyncio.Server) -> uvicorn.Server:
    """A started uvicorn server whose listeners are ``listeners`` (none: it accepts no connection)."""
    server = uvicorn.Server(uvicorn.Config(app=_unserved_app))
    server.servers = list(listeners)
    server.started = True
    return server


@pytest.mark.parametrize(
    ("first", "then"), [(signal.SIGTERM, signal.SIGINT), (signal.SIGINT, signal.SIGTERM)], ids=["SIGTERM", "SIGINT"]
)
async def test_the_first_signal_observes_the_stop_once_and_starts_the_drain_once_behind_the_uvicorn_handler(
    monkeypatch: pytest.MonkeyPatch, signal_handlers_restored: None, first: signal.Signals, then: signal.Signals
) -> None:
    server = _RecordingServer()
    for sig in HANDLED_SIGNALS:
        signal.signal(sig, server.handle_exit)
    drains: list[uvicorn.Server] = []
    drained = asyncio.Event()

    async def drain(drained_server: uvicorn.Server) -> None:
        drains.append(drained_server)
        drained.set()

    monkeypatch.setattr(serve_exit, "end_streams_after_in_flight_replies", drain)

    stops_began = _StopsBegan()
    arm_serve_exit(ExitDrain(), stops_began)
    signal.raise_signal(first)
    signal.raise_signal(first)
    signal.raise_signal(then)
    await asyncio.wait_for(drained.wait(), REQUEST_TIMEOUT_S)
    for _ in range(5):
        await asyncio.sleep(0)

    assert server.exits == [first, first, then]
    # The first signal, either one, is the stop's start: the worker observes it once, and the
    # drain starts once.
    assert stops_began == [f"{first.name} received"]
    assert drains == [server]
    assert AppStatus.enable_automatic_graceful_drain is False


async def test_arming_chains_behind_the_unpatched_handler_a_capture_installed_before_the_library_patched_uvicorn(
    monkeypatch: pytest.MonkeyPatch, signal_handlers_restored: None
) -> None:
    # A worker uvicorn spawns installs its capture before the app import first imports the
    # response library, whose import then replaces ``Server.handle_exit`` on the class: the
    # installed handler is the server's bound method of uvicorn's own function.
    exits: list[int] = []

    def uvicorn_handle_exit(self: uvicorn.Server, sig: int, frame: object) -> None:
        exits.append(sig)

    monkeypatch.setattr(uvicorn.Server, "handle_exit", uvicorn_handle_exit)
    server = uvicorn.Server(uvicorn.Config(app=_unserved_app))
    for sig in HANDLED_SIGNALS:
        signal.signal(sig, server.handle_exit)
    monkeypatch.setattr(AppStatus, "original_handler", uvicorn_handle_exit)
    monkeypatch.setattr(uvicorn.Server, "handle_exit", AppStatus.handle_exit)
    drained = asyncio.Event()

    async def drain(_server: uvicorn.Server) -> None:
        drained.set()

    monkeypatch.setattr(serve_exit, "end_streams_after_in_flight_replies", drain)

    arm_serve_exit(ExitDrain(), _StopsBegan())
    signal.raise_signal(signal.SIGTERM)
    await asyncio.wait_for(drained.wait(), REQUEST_TIMEOUT_S)

    assert exits == [signal.SIGTERM]
    assert AppStatus.should_exit is False


async def test_arming_declares_the_servers_graceful_shutdown_cap_as_the_exit_drain(
    monkeypatch: pytest.MonkeyPatch, signal_handlers_restored: None
) -> None:
    server = uvicorn.Server(uvicorn.Config(app=_unserved_app, timeout_graceful_shutdown=45))
    for sig in HANDLED_SIGNALS:
        signal.signal(sig, server.handle_exit)
    exit_drain = ExitDrain()

    arm_serve_exit(exit_drain, _StopsBegan())

    # Read at call time: the cap the server stops under.
    assert exit_drain.seconds() == 45
    server.config.timeout_graceful_shutdown = None
    assert exit_drain.seconds() is None


async def test_the_announced_drain_adds_the_apps_shutdown_bounds_to_the_servers_cap(
    monkeypatch: pytest.MonkeyPatch, signal_handlers_restored: None
) -> None:
    # The cap bounds only the server's own stop phase; the app's lifespan shutdown runs after it.
    server = uvicorn.Server(uvicorn.Config(app=_unserved_app, timeout_graceful_shutdown=45))
    for sig in HANDLED_SIGNALS:
        signal.signal(sig, server.handle_exit)
    exit_drain = ExitDrain()
    exit_drain.declare_shutdown("module", lambda: 7.0)

    arm_serve_exit(exit_drain, _StopsBegan())

    assert exit_drain.seconds() == 52
    server.config.timeout_graceful_shutdown = None
    assert exit_drain.seconds() is None


async def test_arming_raises_and_keeps_the_library_drain_when_uvicorn_does_not_handle_the_signal(
    signal_handlers_restored: None,
) -> None:
    server = _RecordingServer()
    signal.signal(signal.SIGINT, server.handle_exit)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: None)

    with pytest.raises(RuntimeError, match="SIGTERM"):
        arm_serve_exit(ExitDrain(), _StopsBegan())

    assert AppStatus.enable_automatic_graceful_drain is True
    assert signal.getsignal(signal.SIGINT) == server.handle_exit


async def test_the_drain_ends_the_streams_only_once_the_server_stopped_accepting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serve_exit, "current_epoch_or_none", lambda: None)
    listener = await asyncio.start_server(lambda _reader, _writer: None, "127.0.0.1", 0)

    drain = asyncio.create_task(serve_exit.end_streams_after_in_flight_replies(_started_server(listener)))
    await asyncio.sleep(serve_exit._LISTENERS_POLL_S * 3)
    assert not drain.done()
    assert AppStatus.should_exit is False

    listener.close()
    await asyncio.wait_for(drain, REQUEST_TIMEOUT_S)
    await listener.wait_closed()
    assert AppStatus.should_exit is True


async def test_the_drain_signalled_during_startup_waits_until_the_server_started_and_stopped_accepting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A signal during the lifespan startup reaches the drain before uvicorn opened its listeners;
    # uvicorn then finishes its startup and goes straight to its shutdown.
    monkeypatch.setattr(serve_exit, "current_epoch_or_none", lambda: None)
    server = uvicorn.Server(uvicorn.Config(app=_unserved_app))

    drain = asyncio.create_task(serve_exit.end_streams_after_in_flight_replies(server))
    await asyncio.sleep(serve_exit._LISTENERS_POLL_S * 3)
    assert not drain.done()

    listener = await asyncio.start_server(lambda _reader, _writer: None, "127.0.0.1", 0)
    server.servers = [listener]
    server.started = True
    await asyncio.sleep(serve_exit._LISTENERS_POLL_S * 3)
    assert not drain.done()
    assert AppStatus.should_exit is False

    listener.close()
    await asyncio.wait_for(drain, REQUEST_TIMEOUT_S)
    await listener.wait_closed()
    assert AppStatus.should_exit is True


async def test_the_drain_waits_for_every_deferred_retire_tail_before_ending_the_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def tail() -> None:
        await release.wait()

    running_tail = asyncio.create_task(tail())
    monkeypatch.setattr(serve_exit, "current_epoch_or_none", lambda: None)
    monkeypatch.setattr(serve_exit, "deferred_retires", lambda: frozenset(t for t in [running_tail] if not t.done()))

    drain = asyncio.create_task(serve_exit.end_streams_after_in_flight_replies(_started_server()))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not drain.done()
    assert AppStatus.should_exit is False

    release.set()
    await asyncio.wait_for(drain, REQUEST_TIMEOUT_S)
    assert AppStatus.should_exit is True


async def test_a_failed_drain_is_logged_and_leaves_the_streams_to_the_shutdown_cap(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def failing(_server: uvicorn.Server) -> None:
        raise RuntimeError("drain broke")

    monkeypatch.setattr(serve_exit, "end_streams_after_in_flight_replies", failing)

    serve_exit._start_drain(_started_server())
    (task,) = serve_exit._drains
    await asyncio.wait([task])
    await asyncio.sleep(0)

    assert serve_exit._drains == set()
    assert AppStatus.should_exit is False
    (record,) = [r for r in caplog.records if r.name == serve_exit.__name__]
    assert record.levelname == "ERROR"
    assert "graceful-shutdown cap" in record.getMessage()
    assert record.exc_info is not None
    assert str(record.exc_info[1]) == "drain broke"


async def test_the_drain_lets_a_reload_in_progress_turn_over_before_ending_the_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(epoch_mod, "_current", None)
    held = asyncio.Event()
    turn_over = asyncio.Event()

    async def reload() -> None:
        async with reload_gate.synchronized():
            held.set()
            await turn_over.wait()

    reloading = asyncio.create_task(reload())
    await asyncio.wait_for(held.wait(), REQUEST_TIMEOUT_S)
    drain = asyncio.create_task(serve_exit.end_streams_after_in_flight_replies(_started_server()))
    await _turn_loop()
    assert not drain.done()
    assert AppStatus.should_exit is False

    turn_over.set()
    await asyncio.wait_for(drain, REQUEST_TIMEOUT_S)
    await reloading
    assert AppStatus.should_exit is True


async def test_the_drain_waits_on_a_generation_swapped_in_while_it_waited_on_the_previous_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = Epoch(number=1)
    previous.admit()
    monkeypatch.setattr(epoch_mod, "_current", previous)
    drain = asyncio.create_task(serve_exit.end_streams_after_in_flight_replies(_started_server()))
    await _turn_loop()
    assert not drain.done()

    swapped_in = Epoch(number=2)
    swapped_in.admit()
    epoch_mod._current = swapped_in
    previous.release()
    await _turn_loop()
    assert not drain.done()
    assert AppStatus.should_exit is False

    swapped_in.release()
    await asyncio.wait_for(drain, REQUEST_TIMEOUT_S)
    assert AppStatus.should_exit is True


def _enrolled_run(monkeypatch: pytest.MonkeyPatch, release: asyncio.Event, *, epoch: int) -> asyncio.Task[None]:
    """An in-flight supervised run admitted by generation ``epoch``, running until ``release`` is set."""
    from tai42_skeleton.operations.tool_runs import supervisor

    async def run() -> None:
        await release.wait()

    task = asyncio.create_task(run())
    monkeypatch.setattr(supervisor, "_SUPERVISORS", {task})
    monkeypatch.setattr(supervisor, "_SUPERVISOR_EPOCH", {task: epoch})
    return task


async def test_the_drain_waits_for_an_in_process_run_of_the_serving_generation_before_ending_the_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(epoch_mod, "_current", Epoch(number=3))
    release = asyncio.Event()
    run = _enrolled_run(monkeypatch, release, epoch=3)

    drain = asyncio.create_task(serve_exit.end_streams_after_in_flight_replies(_started_server()))
    await _turn_loop()
    assert not drain.done()
    assert AppStatus.should_exit is False

    release.set()
    await asyncio.wait_for(drain, REQUEST_TIMEOUT_S)
    assert run.done()
    assert AppStatus.should_exit is True


async def test_a_run_of_a_retired_generation_does_not_hold_the_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epoch_mod, "_current", Epoch(number=3))
    release = asyncio.Event()
    run = _enrolled_run(monkeypatch, release, epoch=2)

    await asyncio.wait_for(serve_exit.end_streams_after_in_flight_replies(_started_server()), REQUEST_TIMEOUT_S)

    assert AppStatus.should_exit is True
    assert not run.done()
    release.set()
    await run


class _RecordedLifespan:
    """The lifespan uvicorn's stop ends with: its shutdown cancels the in-flight runs, so it records when it ran."""

    def __init__(self, run: asyncio.Task[None]) -> None:
        self._run = run
        self.run_done_at_shutdown: bool | None = None

    async def shutdown(self) -> None:
        self.run_done_at_shutdown = self._run.done()


async def test_uvicorn_reaches_the_lifespan_shutdown_only_after_the_in_process_run_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No connection is open while the run works (it was submitted and answered 202): the stop
    # still waits for the run, within uvicorn's graceful-shutdown cap, before the lifespan
    # shutdown that cuts every run still in flight.
    monkeypatch.setattr(epoch_mod, "_current", Epoch(number=3))
    release = asyncio.Event()
    run = _enrolled_run(monkeypatch, release, epoch=3)
    server = uvicorn.Server(uvicorn.Config(app=_unserved_app, timeout_graceful_shutdown=SHUTDOWN_CAP_S))
    server.servers = []
    server.started = True
    lifespan = _RecordedLifespan(run)
    server.lifespan = lifespan  # type: ignore[assignment]

    serve_exit._start_drain(server)
    stop = asyncio.create_task(server.shutdown())
    await asyncio.sleep(HAND_OFF_S)
    assert not stop.done()
    assert lifespan.run_done_at_shutdown is None

    release.set()
    await asyncio.wait_for(stop, REQUEST_TIMEOUT_S)
    assert lifespan.run_done_at_shutdown is True
    assert AppStatus.should_exit is True
