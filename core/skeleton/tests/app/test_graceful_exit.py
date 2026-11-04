"""A worker's departure: one idempotent latch, the single SIGTERM a process life sends itself.

Every door that decides a worker must leave ends at its exit latch: the first decision to leave
delivers ONE SIGTERM to the process's own pid, so its already-installed graceful handler
finishes in-flight work before a clean exit; every later one sends nothing.
:func:`depart` is the writer's own exit: the member marks itself ``recycling`` and pulls the
latch once no other change holds it. A forked job child never signals a pid. The recycle handler
answers ``already_leaving`` when the latch is set and arms nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import fakeredis
import pytest
from fakeredis import aioredis
from tai42_contract.app import tai42_app

import tai42_skeleton.app.bus as bus_module
import tai42_skeleton.app.bus.worker_bus as worker_bus_module
from tai42_skeleton.app.bus import (
    WorkerBus,
    WorkerIdentity,
    WorkerKind,
    WorkerState,
    _PresenceValue,
)
from tai42_skeleton.app.bus_settings import BusRedisSettings, BusSettings
from tai42_skeleton.app.graceful_exit import depart
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin

tai42_app.bind(app)

_NOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def kills(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Capture ``os.kill`` instead of actually signalling a process."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(worker_bus_module.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    return calls


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> fakeredis.FakeServer:
    """One fake Redis server every bus connection opens onto."""
    fake_server = fakeredis.FakeServer()

    @asynccontextmanager
    async def fake_ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        client = aioredis.FakeRedis(server=fake_server, decode_responses=True)
        try:
            yield client
        finally:
            await client.aclose()

    monkeypatch.setattr(bus_module, "client_ctx", fake_ctx)
    return fake_server


async def _member_bus(
    server: fakeredis.FakeServer, kind: WorkerKind = WorkerKind.backend, *, drain: float | None = None
) -> WorkerBus:
    """A claimed, presence-holding member bus on ``server`` whose process declared ``drain`` as its exit drain."""
    bus = WorkerBus(BusSettings(redis=BusRedisSettings(redis_url="redis://fake")), kind=kind, exit_drain=lambda: drain)
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    await bus._establish_identity(client)
    bus._presence = _PresenceValue(
        kind=kind,
        pid=os.getpid(),
        generation=bus.identity.generation,
        joined_at=_NOW,
        beat_at=_NOW,
        state=WorkerState.ready,
        deployment_keys=(),
        env_readers="",
    )
    await client.aclose()
    return bus


async def _presence_state(bus: WorkerBus) -> WorkerState:
    rows = {row.name: row for row in await bus.census()}
    return rows[bus.identity.name].state


async def _announced(bus: WorkerBus) -> tuple[WorkerState, float | None]:
    """The state and the drain this worker's census row announces."""
    rows = {row.name: row for row in await bus.census()}
    row = rows[bus.identity.name]
    return row.state, row.drain_seconds


async def _server_keys(server: fakeredis.FakeServer) -> list[str]:
    """Every key on ``server``."""
    client = aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        return sorted(await client.keys("*"))
    finally:
        await client.aclose()


class _Lifecycle(TaiMCPLifecycleMixin):
    """Concrete-enough lifecycle for the op dispatch: no server, no config manager."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preset_manager = cast("Any", SimpleNamespace(reconcile_bases=AsyncMock()))

    def _mcp_tools(self, config: Any, tools: Any, preset_names: frozenset[str]) -> None:  # pragma: no cover - unused
        return None


class _Replies:
    """The reply channel a delivered op answers on, recording each reply frame; delegates the
    rest (the renew-gated presence stamp) to a fake Redis client on the same server."""

    def __init__(self, server: fakeredis.FakeServer, *, fail_terminal: bool = False) -> None:
        self._client = aioredis.FakeRedis(server=server, decode_responses=True)
        self._fail_terminal = fail_terminal
        self.frames: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    async def publish(self, channel: str, data: str) -> int:
        frame = json.loads(data)
        self.frames.append(frame)
        if self._fail_terminal and frame["phase"] == "terminal":
            raise ConnectionError("bus publish failed")
        return 1

    @property
    def terminal_payload(self) -> Any:
        return [f for f in self.frames if f["phase"] == "terminal"][-1].get("payload")


def _recycle_frame() -> dict[str, Any]:
    # A foreign publisher, so the op is applied rather than echo-skipped.
    frame = {"payload": {"op": "recycle"}, "name": "serve-other", "generation": 1, "op_id": "op-x", "reply_to": "r"}
    return {"data": json.dumps(frame)}


async def _deliver_recycle(lifecycle: _Lifecycle, replies: _Replies) -> Any:
    """Deliver one ``Recycle`` op through the subscription loop's handler; its terminal payload."""
    bus = lifecycle.bus
    await bus._handle_op(
        cast("Any", replies), lifecycle._apply_bus_op, _recycle_frame(), failure_payload=lambda _op, _failure: None
    )
    return replies.terminal_payload


# -- the latch -----------------------------------------------------------------


async def test_the_first_request_signals_once_and_every_later_one_sends_nothing(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]]
) -> None:
    bus = await _member_bus(server)
    assert bus.departing is None

    assert bus.request_graceful_exit("first") is True
    assert bus.request_graceful_exit("second") is False

    assert kills == [(os.getpid(), signal.SIGTERM)]
    assert bus.departing == "first"


async def test_a_request_begins_the_leaving_on_the_census_with_its_drain_and_a_repeat_writes_nothing(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]]
) -> None:
    # The decision and its census announce flip together: once the request returns, a roll that
    # reads the row sees the worker leaving with the drain it declared, and finds the latch pulled.
    bus = await _member_bus(server, drain=630.0)

    assert bus.request_graceful_exit("a perpetual task died") is True
    await bus.leaving_announced()

    assert await _announced(bus) == (WorkerState.recycling, 630.0)
    assert bus.departing == "a perpetual task died"
    assert kills == [(os.getpid(), signal.SIGTERM)]

    assert bus.request_graceful_exit("again") is False
    await bus.leaving_announced()
    assert await _announced(bus) == (WorkerState.recycling, 630.0)
    assert kills == [(os.getpid(), signal.SIGTERM)]


# -- a stop decided outside the process --------------------------------------------


async def test_a_stop_that_began_outside_pulls_the_latch_and_announces_without_a_signal(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]]
) -> None:
    lifecycle = _Lifecycle()
    lifecycle._bus = bus = await _member_bus(server, drain=630.0)

    assert lifecycle.stop_began("SIGTERM received") is True
    await bus.leaving_announced()

    assert await _announced(bus) == (WorkerState.recycling, 630.0)
    assert bus.departing == "SIGTERM received"
    assert kills == []
    # A roll reaching the worker during the stop finds it already leaving and arms nothing.
    payload = await _deliver_recycle(lifecycle, _Replies(server))
    assert payload == {"recycling": "backend", "already_leaving": "SIGTERM received"}
    assert kills == []
    # A later decision to leave sends nothing either: the stop is under way.
    assert bus.request_graceful_exit("a perpetual task died") is False
    assert kills == []


async def test_a_second_stop_signal_begins_nothing_and_says_so(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]], caplog: pytest.LogCaptureFixture
) -> None:
    bus = await _member_bus(server)
    assert bus.stop_began("SIGTERM received") is True
    with caplog.at_level("INFO", logger=worker_bus_module.__name__):
        assert bus.stop_began("shutdown") is False
    (line,) = [r.message for r in caplog.records if "already leaving" in r.message]
    assert "SIGTERM received" in line
    assert "shutdown" in line
    assert bus.departing == "SIGTERM received"
    assert kills == []


async def test_a_stop_in_a_forked_job_child_begins_nothing_and_says_so(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]], caplog: pytest.LogCaptureFixture
) -> None:
    bus = WorkerBus(
        BusSettings(redis=BusRedisSettings(redis_url="redis://fake")), kind=WorkerKind.backend, exit_drain=lambda: None
    )
    bus._identity = WorkerIdentity(name="backend-1", kind=WorkerKind.backend, pid=11, generation=3)
    bus._fork_child_nonmember()

    with caplog.at_level("INFO", logger=worker_bus_module.__name__):
        assert bus.stop_began("SIGTERM received") is False
    await bus.leaving_announced()

    assert [r.levelname for r in caplog.records if "SIGTERM received" in r.message] == ["INFO"]
    assert bus.departing is None
    assert kills == []
    assert await _server_keys(server) == []


async def test_a_stop_before_the_bus_is_built_begins_nothing_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    lifecycle = _Lifecycle()
    assert lifecycle._bus is None

    with caplog.at_level("INFO"):
        assert lifecycle.stop_began("SIGTERM received") is False

    assert [r.levelname for r in caplog.records if "SIGTERM received" in r.message] == ["INFO"]


async def test_a_repeat_request_logs_that_the_worker_is_already_leaving(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]], caplog: pytest.LogCaptureFixture
) -> None:
    bus = await _member_bus(server)
    bus.request_graceful_exit("first")
    with caplog.at_level("INFO", logger=worker_bus_module.__name__):
        bus.request_graceful_exit("second")
    (line,) = [r.message for r in caplog.records if "already leaving" in r.message]
    assert "first" in line
    assert "second" in line


async def test_a_signal_that_fails_to_send_raises_and_leaves_the_latch_unset(
    server: fakeredis.FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = await _member_bus(server)

    def refuse(pid: int, sig: int) -> None:
        raise PermissionError("not permitted")

    monkeypatch.setattr(worker_bus_module.os, "kill", refuse)
    with pytest.raises(PermissionError):
        bus.request_graceful_exit("first")
    assert bus.departing is None
    # A worker that is not leaving announces nothing.
    await asyncio.sleep(0)
    assert bus._presence is not None
    assert bus._presence.state is WorkerState.ready
    with pytest.raises(RuntimeError, match="not leaving"):
        await bus.leaving_announced()

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(worker_bus_module.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert bus.request_graceful_exit("second") is True
    assert sent == [(os.getpid(), signal.SIGTERM)]
    assert bus.departing == "second"


def test_a_forked_job_child_never_signals_its_member(kills: list[tuple[int, int]]) -> None:
    bus = WorkerBus(
        BusSettings(redis=BusRedisSettings(redis_url="redis://fake")), kind=WorkerKind.backend, exit_drain=lambda: None
    )
    bus._identity = WorkerIdentity(name="backend-1", kind=WorkerKind.backend, pid=11, generation=3)
    bus._fork_child_nonmember()

    with pytest.raises(RuntimeError, match="non-member"):
        bus.request_graceful_exit("a job's writer exit")
    assert kills == []
    assert bus.departing is None


# -- the writer's own exit and the recycle op meet at the latch ------------------


async def test_a_writer_exit_then_a_recycle_op_signals_once_and_the_op_answers_already_leaving(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]]
) -> None:
    lifecycle = _Lifecycle()
    lifecycle._bus = bus = await _member_bus(server)

    assert await depart(bus, "a") is True
    # The writer's own exit writes ``recycling`` to presence before its signal.
    assert await _presence_state(bus) == WorkerState.recycling

    payload = await _deliver_recycle(lifecycle, _Replies(server))

    assert payload == {"recycling": "backend", "already_leaving": "a"}
    assert kills == [(os.getpid(), signal.SIGTERM)]


async def test_a_recycle_op_then_a_writer_exit_signals_once(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]]
) -> None:
    lifecycle = _Lifecycle()
    lifecycle._bus = bus = await _member_bus(server)

    payload = await _deliver_recycle(lifecycle, _Replies(server))
    assert payload == {"recycling": "backend"}
    # The slot fired after the terminal reply shipped: one signal.
    assert kills == [(os.getpid(), signal.SIGTERM)]

    assert await depart(bus, "b") is False
    assert kills == [(os.getpid(), signal.SIGTERM)]


async def test_two_recycle_ops_signal_once_and_the_second_answers_already_leaving(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]]
) -> None:
    lifecycle = _Lifecycle()
    lifecycle._bus = await _member_bus(server)

    first = await _deliver_recycle(lifecycle, _Replies(server))
    second = await _deliver_recycle(lifecycle, _Replies(server))

    assert first == {"recycling": "backend"}
    assert second == {"recycling": "backend", "already_leaving": "a fleet recycle op"}
    assert kills == [(os.getpid(), signal.SIGTERM)]


async def test_a_recycle_whose_reply_fails_to_ship_still_leaves_once(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]]
) -> None:
    lifecycle = _Lifecycle()
    lifecycle._bus = bus = await _member_bus(server)

    # The worker decided to leave when it applied the op: the reply's failure propagates, and the
    # departure it decided is still carried out, once.
    with pytest.raises(ConnectionError):
        await _deliver_recycle(lifecycle, _Replies(server, fail_terminal=True))
    assert kills == [(os.getpid(), signal.SIGTERM)]
    assert bus.departing == "a fleet recycle op"

    assert await depart(bus, "after the failed reply") is False
    assert kills == [(os.getpid(), signal.SIGTERM)]


# -- a deferred exit: owed to the worker's own in-flight change -------------------


async def test_a_recycle_op_on_a_worker_whose_exit_is_deferred_arms_nothing_and_answers_exit_deferred(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]], caplog: pytest.LogCaptureFixture
) -> None:
    lifecycle = _Lifecycle()
    lifecycle._bus = bus = await _member_bus(server)
    bus.defer_exit("the env change")

    with caplog.at_level("INFO"):
        payload = await _deliver_recycle(lifecycle, _Replies(server))

    assert payload == {"recycling": "backend", "exit_deferred": "the env change"}
    assert kills == []
    assert bus.departing is None
    assert bus.exit_deferred == "the env change"
    # No presence write: the worker still serves its own change.
    assert await _presence_state(bus) == WorkerState.ready
    assert any("deferred to its own change (the env change)" in r.message for r in caplog.records)


async def test_a_recycle_op_on_a_worker_already_leaving_answers_already_leaving_over_a_deferred_exit(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]]
) -> None:
    lifecycle = _Lifecycle()
    lifecycle._bus = bus = await _member_bus(server)
    bus.defer_exit("the env change")
    bus.request_graceful_exit("first")

    payload = await _deliver_recycle(lifecycle, _Replies(server))

    assert payload == {"recycling": "backend", "already_leaving": "first"}
    assert kills == [(os.getpid(), signal.SIGTERM)]


async def test_the_exit_latch_leaves_every_deferred_exit_to_its_change(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]]
) -> None:
    bus = await _member_bus(server)
    hold = bus.defer_exit("the env change")

    assert bus.request_graceful_exit("first") is True

    # The latch never releases another change's hold: that change still owns it.
    assert bus.exit_deferred == "the env change"
    bus.release_deferred_exit(hold)
    assert bus.exit_deferred is None
    assert bus.departing == "first"


def test_a_deferred_exit_is_held_once_per_deferral_and_released_one_at_a_time() -> None:
    bus = WorkerBus(
        BusSettings(redis=BusRedisSettings(redis_url="redis://fake")), kind=WorkerKind.serve, exit_drain=lambda: None
    )
    bus._identity = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=11, generation=3)
    first = bus.defer_exit("the env change")
    second = bus.defer_exit("the env change")

    bus.release_deferred_exit(first)
    assert bus.exit_deferred == "the env change"
    bus.release_deferred_exit(second)
    assert bus.exit_deferred is None


def test_releasing_a_deferral_twice_raises() -> None:
    bus = WorkerBus(
        BusSettings(redis=BusRedisSettings(redis_url="redis://fake")), kind=WorkerKind.serve, exit_drain=lambda: None
    )
    bus._identity = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=11, generation=3)
    hold = bus.defer_exit("a")
    bus.release_deferred_exit(hold)

    with pytest.raises(RuntimeError, match="is not held"):
        bus.release_deferred_exit(hold)


def test_a_forked_job_child_inherits_no_deferred_exit() -> None:
    bus = WorkerBus(
        BusSettings(redis=BusRedisSettings(redis_url="redis://fake")), kind=WorkerKind.backend, exit_drain=lambda: None
    )
    bus._identity = WorkerIdentity(name="backend-1", kind=WorkerKind.backend, pid=11, generation=3)
    bus.defer_exit("the member's change")
    bus._fork_child_nonmember()

    assert bus.exit_deferred is None
    with pytest.raises(RuntimeError, match="non-member"):
        bus.defer_exit("a child's change")


async def test_a_member_departing_on_its_deferred_exit_signals_once_and_releases_its_hold(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]]
) -> None:
    bus = await _member_bus(server)
    hold = bus.defer_exit("the env change")

    assert await depart(bus, "the writer's own restart", [hold]) is True

    assert kills == [(os.getpid(), signal.SIGTERM)]
    assert bus.exit_deferred is None
    assert bus.departing == "the writer's own restart"
    assert await _presence_state(bus) == WorkerState.recycling


async def test_a_member_departing_waits_for_every_other_held_change_then_exits_once(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]]
) -> None:
    bus = await _member_bus(server)
    own = bus.defer_exit("the first change")
    other = bus.defer_exit("the second change")

    leaving = asyncio.create_task(depart(bus, "the writer's own restart", [own]))
    for _ in range(5):
        await asyncio.sleep(0)

    # The second change still serves its roll and its reply: nothing is signalled or marked yet.
    assert not leaving.done()
    assert kills == []
    assert bus.departing is None
    assert bus.exit_deferred == "the second change"

    second_leaving = asyncio.create_task(depart(bus, "the writer's own restart", [other]))
    # Both changes have answered: exactly one of their exits pulls the latch.
    assert sorted([await leaving, await second_leaving]) == [False, True]

    assert kills == [(os.getpid(), signal.SIGTERM)]
    assert bus.exit_deferred is None
    assert await _presence_state(bus) == WorkerState.recycling


async def test_a_change_holding_the_exit_as_the_last_other_one_is_released_keeps_the_member_waiting(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]]
) -> None:
    bus = await _member_bus(server)
    other = bus.defer_exit("the first change")

    leaving = asyncio.create_task(depart(bus, "the writer's own restart"))
    for _ in range(5):
        await asyncio.sleep(0)
    # The last held change is answered, and in the same step a new change holds the exit.
    bus.release_deferred_exit(other)
    newer = bus.defer_exit("the second change")
    for _ in range(5):
        await asyncio.sleep(0)

    assert not leaving.done()
    assert kills == []
    assert bus.departing is None
    assert bus._presence is not None
    assert bus._presence.state == WorkerState.ready

    bus.release_deferred_exit(newer)
    assert await asyncio.wait_for(leaving, timeout=5) is True
    assert kills == [(os.getpid(), signal.SIGTERM)]


async def test_a_member_whose_recycling_mark_fails_releases_its_hold_still_leaves_and_raises(
    server: fakeredis.FakeServer, kills: list[tuple[int, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = await _member_bus(server)
    hold = bus.defer_exit("the env change")
    monkeypatch.setattr(bus, "mark_recycling", AsyncMock(side_effect=ConnectionError("presence write lost")))

    # The decision to leave came before the presence write: the departure is carried out, and the
    # write's failure raises.
    with pytest.raises(ConnectionError, match="presence write lost"):
        await depart(bus, "the writer's own restart", [hold])

    assert bus.exit_deferred is None
    assert bus.departing == "the writer's own restart"
    assert kills == [(os.getpid(), signal.SIGTERM)]
