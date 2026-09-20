"""The cross-worker per-thread turn mutex (:mod:`tai42_skeleton.conversations.thread_lease`).

Two ``TurnCaps`` instances sharing one fake Redis stand in for two workers: their per-worker
FIFOs are independent, so the ONLY thing serializing a turn on the same thread across them is
the Redis lease. The fake ages a lease's TTL through its ``advance`` time-travel, and the
lease module's ``asyncio.sleep`` is replaced by a controllable clock so the heartbeat and the
acquisition poll step deterministically.
"""

from __future__ import annotations

import asyncio as _aio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from itertools import count

import pytest
from tai42_contract.conversations import ConversationRoute

from tai42_skeleton.agent.thread_reservation import PERSON_THREAD_PREFIX
from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import thread_lease as thread_lease_module
from tai42_skeleton.conversations.caps import TurnCaps
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.thread_lease import ThreadLeaseLostError, ThreadTurnLease
from tai42_skeleton.conversations.turn import operator_send
from tai42_skeleton.operations import conversations as ops

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

_THREAD = "bridge:line:+15550002222"


class _NoLease:
    """A lease disabled to no-op, used to show the lost update the mutex prevents."""

    def reconfigure(self, settings: ConversationsSettings) -> None:
        pass

    @asynccontextmanager
    async def held(self, thread_id: str) -> AsyncIterator[None]:
        yield


class _ShimAsyncio:
    """The lease module's ``asyncio``, with ``sleep`` routed to a test clock and every other
    name delegated to the real module."""

    def __init__(self, sleep) -> None:
        self.sleep = sleep

    def __getattr__(self, name: str):
        return getattr(_aio, name)


class _Clock:
    """The lease module's clock: the acquisition poll retries at once, and each heartbeat tick
    is granted one at a time so a test can interleave TTL time-travel between refreshes."""

    def __init__(self, refresh_seconds: float, poll_seconds: float) -> None:
        self._refresh = refresh_seconds
        self._poll = poll_seconds
        self._beats: _aio.Queue[None] = _aio.Queue()

    async def sleep(self, delay: float) -> None:
        if delay == self._refresh:
            await self._beats.get()
        else:
            await _aio.sleep(0)

    async def beat(self) -> None:
        """Grant exactly one heartbeat refresh and let it run to completion."""
        await self._beats.put(None)
        for _ in range(10):
            await _aio.sleep(0)


class _FakeMonotonic:
    """The lease module's ``monotonic``, driven by the test so the heartbeat's lost-hold
    deadline is crossed on command rather than by wall-clock time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _wire(monkeypatch, fake: FakeRecordRedis) -> None:
    ctx = make_record_client_ctx(fake)
    monkeypatch.setattr(records_module, "client_ctx", ctx)
    monkeypatch.setattr(thread_lease_module, "client_ctx", ctx)


@pytest.fixture
def fake(monkeypatch) -> FakeRecordRedis:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("CONVERSATIONS_THREAD_LEASE_POLL_SECONDS", "0.02")
    caps_module._CAPS_CACHE.clear()
    f = FakeRecordRedis()
    _wire(monkeypatch, f)
    return f


async def _worker(
    caps: TurnCaps,
    worker_id: str,
    memory: dict[str, list[str]],
    spans: list[tuple[str, int, int]],
    order,
    hold: _aio.Event,
    entered: _aio.Event,
    thread_id: str = _THREAD,
) -> None:
    """One worker's turn: forks the thread's shared memory (read parent, append child, write
    back), pausing on ``hold`` so a sibling worker's turn overlaps it if nothing serializes
    them. Records its start/end so a test can assert the spans do not overlap."""
    caps.reserve_thread_slot(thread_id)
    async with caps.run_reserved(thread_id):
        start = next(order)
        entered.set()
        parent = list(memory["messages"])
        await hold.wait()
        parent.append(worker_id)
        memory["messages"] = parent
        spans.append((worker_id, start, next(order)))


async def test_headline_two_workers_serialize_and_keep_both_turns(fake):
    # Two workers, one thread: the lease serializes their turns, so the second reads the
    # first's write and the thread memory keeps BOTH turns' messages.
    worker_a, worker_b = TurnCaps(ConversationsSettings()), TurnCaps(ConversationsSettings())
    memory: dict[str, list[str]] = {"messages": []}
    spans: list[tuple[str, int, int]] = []
    order = count()
    hold = _aio.Event()
    a_in, b_in = _aio.Event(), _aio.Event()

    ta = _aio.create_task(_worker(worker_a, "A", memory, spans, order, hold, a_in))
    tb = _aio.create_task(_worker(worker_b, "B", memory, spans, order, hold, b_in))
    await _aio.wait_for(a_in.wait(), 1)
    # A holds the lease; B is polling for it and cannot enter — its turn has not started.
    await _aio.sleep(0.1)
    assert not b_in.is_set()

    hold.set()
    await _aio.wait_for(_aio.gather(ta, tb), 2)

    # b. both turns' messages survived — the lost update the bug produces did not happen.
    assert memory["messages"] == ["A", "B"]
    # a. the spans do not overlap: A's turn ends before B's begins.
    span_a = next(s for s in spans if s[0] == "A")
    span_b = next(s for s in spans if s[0] == "B")
    assert span_a[2] <= span_b[1]


async def test_headline_without_the_lease_loses_an_update(fake):
    # The red half: with the lease disabled, both workers fork the SAME empty parent and the
    # last write wins, dropping one turn's message.
    worker_a, worker_b = TurnCaps(ConversationsSettings()), TurnCaps(ConversationsSettings())
    worker_a._lease, worker_b._lease = _NoLease(), _NoLease()  # pyright: ignore[reportAttributeAccessIssue]
    memory: dict[str, list[str]] = {"messages": []}
    spans: list[tuple[str, int, int]] = []
    order = count()
    hold = _aio.Event()
    a_in, b_in = _aio.Event(), _aio.Event()

    ta = _aio.create_task(_worker(worker_a, "A", memory, spans, order, hold, a_in))
    tb = _aio.create_task(_worker(worker_b, "B", memory, spans, order, hold, b_in))
    await _aio.wait_for(a_in.wait(), 1)
    await _aio.wait_for(b_in.wait(), 1)
    # Both turns entered concurrently and read the same empty parent.
    await _aio.sleep(0.05)
    hold.set()
    await _aio.wait_for(_aio.gather(ta, tb), 2)

    # One update was lost: only a single turn's message survives.
    assert len(memory["messages"]) == 1


async def test_interrupt_spanning_turn_holds_the_lease_across_its_heartbeats(fake, monkeypatch):
    # A turn paused far longer than one lease stays exclusive because its heartbeat keeps
    # refreshing the lease; a sibling worker never acquires until it releases.
    monkeypatch.setenv("CONVERSATIONS_THREAD_LEASE_SECONDS", "2")
    monkeypatch.setenv("CONVERSATIONS_THREAD_LEASE_REFRESH_SECONDS", "1")
    clock = _Clock(refresh_seconds=1, poll_seconds=ConversationsSettings().thread_lease_poll_seconds)
    monkeypatch.setattr(thread_lease_module, "asyncio", _ShimAsyncio(clock.sleep))

    worker_a, worker_b = TurnCaps(ConversationsSettings()), TurnCaps(ConversationsSettings())
    memory: dict[str, list[str]] = {"messages": []}
    spans: list[tuple[str, int, int]] = []
    order = count()
    hold = _aio.Event()
    a_in, b_in = _aio.Event(), _aio.Event()

    ta = _aio.create_task(_worker(worker_a, "A", memory, spans, order, hold, a_in))
    tb = _aio.create_task(_worker(worker_b, "B", memory, spans, order, hold, b_in))
    await _aio.wait_for(a_in.wait(), 1)

    # Age the lease most of the way to expiry, then let the heartbeat refresh it — three times
    # over, well past one lease. Each refresh resets the TTL before B's poll can find it gone.
    for _ in range(3):
        fake.advance(1.2)
        await clock.beat()
        assert not b_in.is_set()

    hold.set()
    await _aio.wait_for(_aio.gather(ta, tb), 2)
    assert b_in.is_set()
    assert memory["messages"] == ["A", "B"]


async def test_crash_steal_lost_lease_cancels_the_turn(fake, monkeypatch):
    # A holder whose heartbeat has stopped loses the lease on TTL expiry; a second worker
    # adopts it and completes, and the stale holder's next refresh reports the loss, cancelling
    # its turn with ThreadLeaseLostError — one error outcome, no fork.
    monkeypatch.setenv("CONVERSATIONS_THREAD_LEASE_SECONDS", "2")
    monkeypatch.setenv("CONVERSATIONS_THREAD_LEASE_REFRESH_SECONDS", "1")
    clock = _Clock(refresh_seconds=1, poll_seconds=ConversationsSettings().thread_lease_poll_seconds)
    monkeypatch.setattr(thread_lease_module, "asyncio", _ShimAsyncio(clock.sleep))

    worker_a, worker_b = TurnCaps(ConversationsSettings()), TurnCaps(ConversationsSettings())
    memory: dict[str, list[str]] = {"messages": []}
    stuck = _aio.Event()
    a_in = _aio.Event()

    async def _stalled_a() -> None:
        worker_a.reserve_thread_slot(_THREAD)
        async with worker_a.run_reserved(_THREAD):
            a_in.set()
            await stuck.wait()  # never set: A only leaves by the lost-lease cancel
            memory["messages"].append("A")

    ta = _aio.create_task(_stalled_a())
    await _aio.wait_for(a_in.wait(), 1)

    # A's heartbeat is never granted a tick (crashed); its lease lapses.
    fake.advance(2.0)

    # A second worker adopts the free lease and completes.
    hold_b = _aio.Event()
    hold_b.set()
    tb = _aio.create_task(_worker(worker_b, "B", memory, [], count(), hold_b, _aio.Event()))
    await _aio.wait_for(tb, 2)
    assert memory["messages"] == ["B"]

    # A's next refresh sees the lease is no longer its own and cancels the turn.
    await clock.beat()
    with pytest.raises(ThreadLeaseLostError):
        await _aio.wait_for(ta, 2)
    # Exactly one outcome: B's write, never A's.
    assert memory["messages"] == ["B"]


async def test_partition_holder_self_cancels_once_the_lease_can_no_longer_be_proven(fake, monkeypatch):
    # A Redis partition: the holder's every heartbeat raises, so it never learns the lease is
    # gone from a returned-0. Without a deadline it retries forever against a lease that has
    # already lapsed server-side and been adopted — the exact cross-worker fork the mutex
    # exists to prevent. With the deadline it STOPS once monotonic time passes
    # thread_lease_seconds from its last proven refresh, self-cancelling with
    # ThreadLeaseLostError.
    monkeypatch.setenv("CONVERSATIONS_THREAD_LEASE_SECONDS", "2")
    monkeypatch.setenv("CONVERSATIONS_THREAD_LEASE_REFRESH_SECONDS", "1")
    clock = _Clock(refresh_seconds=1, poll_seconds=ConversationsSettings().thread_lease_poll_seconds)
    monkeypatch.setattr(thread_lease_module, "asyncio", _ShimAsyncio(clock.sleep))
    mono = _FakeMonotonic()
    monkeypatch.setattr(thread_lease_module, "monotonic", mono)

    # Every lease refresh raises (the partition); acquire, poll, and release still reach the
    # fake, so a peer can adopt the lapsed lease.
    real_eval = fake.eval

    async def _partitioned_eval(script, numkeys, *keys_and_args):
        if "conversations:thread_lease:refresh" in script:
            raise ConnectionError("simulated redis partition")
        return await real_eval(script, numkeys, *keys_and_args)

    monkeypatch.setattr(fake, "eval", _partitioned_eval)

    worker_a, worker_b = TurnCaps(ConversationsSettings()), TurnCaps(ConversationsSettings())
    memory: dict[str, list[str]] = {"messages": []}
    a_in = _aio.Event()
    stuck = _aio.Event()

    async def _stalled_a() -> None:
        worker_a.reserve_thread_slot(_THREAD)
        async with worker_a.run_reserved(_THREAD):
            a_in.set()
            await stuck.wait()  # never set: A only leaves by the lost-lease cancel
            memory["messages"].append("A")

    ta = _aio.create_task(_stalled_a())
    await _aio.wait_for(a_in.wait(), 1)

    # One second in: the refresh raises, but the lease is still under its full TTL, so the
    # holder logs and retries — it does NOT self-cancel.
    mono.advance(1.0)
    await clock.beat()
    assert not ta.done()

    # The lease's TTL lapses and a second worker adopts the free key via SET NX and completes.
    fake.advance(2.0)
    hold_b = _aio.Event()
    hold_b.set()
    tb = _aio.create_task(_worker(worker_b, "B", memory, [], count(), hold_b, _aio.Event()))
    await _aio.wait_for(tb, 2)
    assert memory["messages"] == ["B"]

    # The next heartbeat crosses thread_lease_seconds since the last proven refresh: the hold
    # can no longer be proven, so the holder self-cancels with ThreadLeaseLostError.
    mono.advance(1.0)
    await clock.beat()
    with pytest.raises(ThreadLeaseLostError):
        await _aio.wait_for(ta, 2)
    # Exactly one outcome: B's write, never A's.
    assert memory["messages"] == ["B"]


async def test_external_cancel_during_cleanup_is_not_swallowed(fake, monkeypatch):
    # A genuine shutdown cancel that lands while ``held`` is tearing down its heartbeat must
    # propagate, not be absorbed by the heartbeat-cleanup suppression — otherwise the turn
    # would finish normally in the face of a real cancellation.
    lease = ThreadTurnLease(ConversationsSettings())
    heartbeat_started = _aio.Event()
    heartbeat_cancelling = _aio.Event()
    release_heartbeat = _aio.Event()

    async def _parked_heartbeat(key, token, owner, signal, last_success):
        heartbeat_started.set()
        try:
            await _aio.Event().wait()
        except _aio.CancelledError:
            # Hold the owner inside its ``await heartbeat`` cleanup window so the test lands an
            # external cancel there deterministically, then finish as cancelled.
            heartbeat_cancelling.set()
            await release_heartbeat.wait()
            raise

    monkeypatch.setattr(lease, "_heartbeat", _parked_heartbeat)

    entered = _aio.Event()
    release_body = _aio.Event()

    async def _hold() -> None:
        async with lease.held(_THREAD):
            entered.set()
            await release_body.wait()

    task = _aio.create_task(_hold())
    await _aio.wait_for(heartbeat_started.wait(), 1)
    await _aio.wait_for(entered.wait(), 1)

    # The body finishes normally; cleanup begins and parks inside the heartbeat cancel window.
    release_body.set()
    await _aio.wait_for(heartbeat_cancelling.wait(), 1)

    # The external cancel lands in the cleanup window, then the heartbeat is let go.
    task.cancel()
    await _aio.sleep(0)
    release_heartbeat.set()

    with pytest.raises(_aio.CancelledError):
        await _aio.wait_for(task, 1)


async def test_release_is_token_guarded(fake):
    # A stale holder's release never deletes the lease a later holder adopted; only the token
    # that owns the lease can delete it.
    settings = ConversationsSettings()
    lease = ThreadTurnLease(settings)
    key = settings.thread_lease_key(_THREAD)
    await fake.set(key, "adopter", px=2000, nx=True)

    await lease._release(key, "stale")
    assert fake._strings.get(key) == "adopter"

    await lease._release(key, "adopter")
    assert key not in fake._strings


# -- door coverage ------------------------------------------------------------------------
#
# Worker A holds the lease on a SEPARATE TurnCaps instance, so the door's own worker (the
# singleton) shares no per-worker lock with it — the only thing that can block the door is the
# cross-worker lease. Each door reaches the lease through ``run_reserved``.


class _RecordingSaver:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


class _FakeCheckpointRegistry:
    def __init__(self, saver: _RecordingSaver) -> None:
        self._saver = saver

    async def get_checkpointer(self, provider: str, conn_string: str | None) -> _RecordingSaver:
        return self._saver


class _FakeProviderSettings:
    checkpoint = "memory"
    checkpoint_conn_string = None


class _PersonStoreGone:
    async def get_by_id(self, person_id: str):
        return None


def _stub_checkpoint(monkeypatch) -> _RecordingSaver:
    import tai42_kit.llm.checkpoint.checkpoint_registry as registry_mod
    import tai42_kit.llm.settings as settings_mod

    saver = _RecordingSaver()
    monkeypatch.setattr(registry_mod, "checkpoint_registry", lambda: _FakeCheckpointRegistry(saver))
    monkeypatch.setattr(settings_mod, "llm_provider_settings", lambda: _FakeProviderSettings())
    return saver


@asynccontextmanager
async def _lease_held_by_a(thread_id: str):
    """A separate worker holds ``thread_id``'s lease for the block's duration."""
    worker_a = TurnCaps(ConversationsSettings())
    holding, release = _aio.Event(), _aio.Event()

    async def _hold() -> None:
        worker_a.reserve_thread_slot(thread_id)
        async with worker_a.run_reserved(thread_id):
            holding.set()
            await release.wait()

    task = _aio.create_task(_hold())
    await _aio.wait_for(holding.wait(), 1)
    caps_module._CAPS_CACHE.clear()  # the door builds a fresh singleton, distinct from worker A
    try:
        yield release
    finally:
        release.set()
        await task


async def test_door_operator_send_blocks_behind_a_turn(fake, monkeypatch):
    route = ConversationRoute(
        route_name="line",
        door="channel",
        target_kind="agent",  # pyright: ignore[reportArgumentType]
        target_name="echo",
        execution_key="svc",
        channel="twilio",
        our_identity="+15550001111",
        execution_key_fingerprint="fp-1",
    )
    async with _lease_held_by_a(_THREAD):
        send = _aio.create_task(
            operator_send(
                route=route,
                thread_id=_THREAD,
                client_address="+15550002222",
                text="on it",
                operator_principal="op-1",
            )
        )
        await _aio.sleep(0.1)
        assert not send.done()  # blocked on the cross-worker lease, not on any local lock
        send.cancel()
        with pytest.raises(_aio.CancelledError):
            await send


async def test_door_delete_thread_blocks_behind_a_turn(fake, monkeypatch):
    saver = _stub_checkpoint(monkeypatch)
    monkeypatch.setattr(ops, "get_conversations_manager", lambda: object())
    thread_id = "bridge:chat:+15550001111"

    async with _lease_held_by_a(thread_id):
        delete = _aio.create_task(ops.delete_conversation_thread("chat", thread_id))
        await _aio.sleep(0.1)
        assert not delete.done()
        assert saver.deleted == []  # nothing torn down while it waits on the lease

    result = await _aio.wait_for(delete, 2)
    assert result == {"removed": 0, "route_name": "chat", "thread_id": thread_id}
    assert saver.deleted == [thread_id]


async def test_door_person_gone_branch_serializes_behind_a_turn(fake, monkeypatch):
    saver = _stub_checkpoint(monkeypatch)
    monkeypatch.setattr(ops, "get_conversations_manager", lambda: object())
    monkeypatch.setattr(ops, "_person_store", lambda: _PersonStoreGone())
    thread_id = f"{PERSON_THREAD_PREFIX}PID1"

    async with _lease_held_by_a(thread_id):
        delete = _aio.create_task(ops.delete_conversation_person("PID1"))
        await _aio.sleep(0.1)
        assert not delete.done()  # the person-gone checkpoint delete waits under the lease too
        assert saver.deleted == []

    result = await _aio.wait_for(delete, 2)
    assert result == {"person_id": "PID1", "removed": 0, "erased": False}
    assert saver.deleted == [thread_id]
