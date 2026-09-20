"""The background recovery sweep: the periodic stalled-delivery pass, the lapsed-intake and
terminal-index passes the loop drives, and the sweep task's start/stop lifecycle.

The sweep decides WHETHER a record is picked up again — only once the dead worker's lease has
lapsed, never while another's is live."""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest
from tai42_contract.channels import ChannelDeliveryError, ChannelInputError

from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import delivery_sweep as delivery_sweep_module
from tai42_skeleton.conversations import ledger as ledger_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

#: Every answer in this module is chunked at this width, so a handful of characters makes
#: a genuinely multi-chunk send.
_CHUNK_CHARS = 10


class WorkerDiedError(RuntimeError):
    """What a channel raises to stand in for the worker vanishing mid-send — not a
    ``ChannelDeliveryError``, so the executor does not turn it into a ``failed`` record."""


class FakeChannel:
    """Records every chunk it is asked to send. ``crash_on`` abandons the send on the nth
    chunk (a dead worker), ``fail_on`` refuses it the way a provider does, ``input_fail_on``
    permanently refuses its shape (a ``ChannelInputError``), and ``hang_on`` never returns
    from it (a send still in flight)."""

    def __init__(
        self,
        prefix: str = "out",
        *,
        crash_on: int | None = None,
        fail_on: int | None = None,
        input_fail_on: int | None = None,
        hang_on: int | None = None,
        watch=None,
    ) -> None:
        self.sends: list[str] = []
        self._prefix = prefix
        self._crash_on = crash_on
        self._fail_on = fail_on
        self._input_fail_on = input_fail_on
        self._hang_on = hang_on
        self._watch = watch

    async def notify(self, notification) -> list[str]:
        self.sends.append(notification.message)
        if self._watch is not None:
            watched = self._watch()
            if inspect.isawaitable(watched):
                await watched
        if self._crash_on is not None and len(self.sends) == self._crash_on:
            raise WorkerDiedError("the worker died mid-send")
        if self._fail_on is not None and len(self.sends) == self._fail_on:
            raise ChannelDeliveryError("the provider refused the chunk")
        if self._input_fail_on is not None and len(self.sends) == self._input_fail_on:
            raise ChannelInputError("the provider cannot render the chunk")
        if self._hang_on is not None and len(self.sends) == self._hang_on:
            await asyncio.Event().wait()
        return [f"{self._prefix}-{len(self.sends)}"]


class _FakeChannels:
    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel

    def get(self, name: str) -> FakeChannel:
        return self._channel


class _FakeDeliveryApp:
    def __init__(self, channel: FakeChannel) -> None:
        self.channels = _FakeChannels(channel)


@pytest.fixture(autouse=True)
def _conversations_env(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_CLAIM_LEASE_SECONDS", "120")
    monkeypatch.setenv("CONVERSATIONS_MAX_MESSAGE_CHARS", f'{{"twilio": {_CHUNK_CHARS}}}')


@pytest.fixture(autouse=True)
def _no_grace_timers(monkeypatch):
    """The hour-long fallback confirmation a completed channel send schedules is a
    different mechanism; stubbing it keeps these tests to the send itself."""

    async def _noop(message_id: str, grace_seconds: float) -> None:
        return None

    monkeypatch.setattr(delivery_module, "_confirm_after_grace", _noop)


@pytest.fixture(autouse=True)
def _drop_leftover_tasks():
    yield
    for task in list(delivery_module._DELIVERY_TASKS):
        task.cancel()
    delivery_module._DELIVERY_TASKS.clear()


@pytest.fixture
def fake(monkeypatch) -> FakeRecordRedis:
    """One faked redis behind both the record store and the send ledger, so a test reads
    the same keyspace the executor writes through."""
    backing = FakeRecordRedis()
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(backing))
    monkeypatch.setattr(ledger_module, "client_ctx", make_record_client_ctx(backing))
    return backing


@pytest.fixture
def store(fake: FakeRecordRedis) -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


def _record(message_id: str, answer: str) -> ConversationRecord:
    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door="channel",
        thread_id=f"bridge:line:{message_id}",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        origin="client",
        inbound_text=f"ask {message_id}",
        answer_status="answered",
        answer=answer,
        created_at=now,
        updated_at=now,
    )


def _wire_channel(monkeypatch, channel) -> None:
    monkeypatch.setattr(delivery_module, "tai42_app", _FakeDeliveryApp(channel))


def _claim(fake: FakeRecordRedis, message_id: str) -> tuple[str, float]:
    """The record's live lease as ``(token, expiry)``; an empty token when it is free."""
    raw = fake._hashes[ConversationsSettings().record_key(message_id)]["claim"]
    if not raw:
        return "", 0.0
    token, expiry = raw.split(":", 1)
    return token, float(expiry)


async def _drain_spawned(store: ConversationRecordStore) -> None:
    """Await every delivery the sweep spawned."""
    pending = [task for task in delivery_module._DELIVERY_TASKS if not task.done()]
    if pending:
        await asyncio.gather(*pending)


async def _get(store: ConversationRecordStore, message_id: str) -> ConversationRecord:
    record = await store.get_record(message_id)
    assert record is not None
    return record


# -- the periodic sweep reclaims what a dead worker left -----------------------


async def test_sweep_redrives_a_record_whose_lease_has_lapsed(monkeypatch, fake, store):
    """A worker took the lease and died before sending anything. Once that lease lapses
    the sweep re-drives the record and the answer finally goes out — the boot re-drive
    alone would have found the lease still live and given up for the life of the process.
    """
    channel = FakeChannel()
    _wire_channel(monkeypatch, channel)
    await store.create_record(_record("m-dead", "the answer"))
    # The dead worker's lease, taken 200s ago under a 120s lease: long lapsed.
    assert await store.claim_delivery("m-dead", time.time() - 200, "dead-worker", 120) == 1

    await delivery_module.sweep_stalled_deliveries()
    await _drain_spawned(store)

    assert channel.sends == ["the answer"]
    assert (await _get(store, "m-dead")).delivery_status is DeliveryStatus.PROVISIONAL


async def test_sweep_leaves_another_workers_live_lease_alone(monkeypatch, fake, store):
    """Under the supported multi-worker deployment a lease found pending may be somebody
    else's send in flight. The sweep must not steal it."""
    channel = FakeChannel()
    _wire_channel(monkeypatch, channel)
    await store.create_record(_record("m-live", "the answer"))
    assert await store.claim_delivery("m-live", time.time(), "other-worker", 120) == 1

    await delivery_module.sweep_stalled_deliveries()
    await _drain_spawned(store)

    assert channel.sends == []
    assert (await _get(store, "m-live")).delivery_status is DeliveryStatus.PENDING_DELIVERY
    assert _claim(fake, "m-live")[0] == "other-worker"


async def test_sweep_confirms_a_provisional_record_past_its_grace(monkeypatch, fake, store):
    """The worker that scheduled the fallback confirmation died holding it, so nothing in
    the process would ever close the record. The sweep does."""
    _wire_channel(monkeypatch, FakeChannel())
    await store.create_record(_record("m-prov", "the answer"))
    await store.mark_provisional("m-prov", ["out-1"], 1, time.time(), "tok")
    fake._hashes[ConversationsSettings().record_key("m-prov")]["grace_deadline"] = str(time.time() - 1)

    await delivery_module.sweep_stalled_deliveries()
    await _drain_spawned(store)

    assert (await _get(store, "m-prov")).delivery_status is DeliveryStatus.DELIVERED


async def test_sweep_does_not_confirm_a_provisional_record_still_within_grace(monkeypatch, fake, store):
    _wire_channel(monkeypatch, FakeChannel())
    await store.create_record(_record("m-young", "the answer"))
    await store.mark_provisional("m-young", ["out-1"], 1, time.time(), "tok")

    await delivery_module.sweep_stalled_deliveries()
    await _drain_spawned(store)

    assert (await _get(store, "m-young")).delivery_status is DeliveryStatus.PROVISIONAL


async def test_sweep_fails_a_record_that_has_spent_every_attempt(monkeypatch, fake, store):
    """A record no re-drive can finish must not be swept forever: once its attempts are
    spent it becomes a loud, retained ``failed`` instead."""
    channel = FakeChannel()
    _wire_channel(monkeypatch, channel)
    await store.create_record(_record("m-spent", "the answer"))
    for _ in range(ConversationsSettings().delivery_max_attempts):
        await store.bump_attempt("m-spent")

    await delivery_module.sweep_stalled_deliveries()
    await _drain_spawned(store)

    assert channel.sends == []
    assert (await _get(store, "m-spent")).delivery_status is DeliveryStatus.FAILED


# -- the periodic loop drives both recovery passes -----------------------------


async def test_the_periodic_loop_runs_every_recovery_pass(monkeypatch):
    """A worker that dies and never reboots leaves its ``accepted`` records and its stale
    terminal-index members to a sibling's PERIODIC passes; a failing pass must not skip the
    rest, or a boot-only re-drive would hold those messages unanswered for every process."""
    ran: list[str] = []

    async def _broken_delivery_pass() -> None:
        ran.append("delivery")
        raise RuntimeError("this pass is broken")

    async def _intake_pass() -> None:
        ran.append("intake")

    async def _prune_pass(self, route_names, cursor=records_module.PRUNE_START):
        ran.append("prune")
        return cursor

    class _Routes:
        async def list_routes(self):
            return {"alpha": object()}

    monkeypatch.setattr(delivery_module, "sweep_stalled_deliveries", _broken_delivery_pass)
    monkeypatch.setattr(turn_module, "redrive_accepted", _intake_pass)
    # The prune pass lists live routes first; stub the manager so it never reaches a real
    # redis and the pass is what runs, not a swallowed connection error.
    monkeypatch.setattr(delivery_module, "get_conversations_manager", _Routes)
    monkeypatch.setattr(records_module.ConversationRecordStore, "prune_expired_terminal_indexes", _prune_pass)

    loop = asyncio.create_task(delivery_sweep_module._sweep_loop(0.01))
    for _ in range(200):
        if ran.count("prune") >= 2:
            break
        await asyncio.sleep(0.01)
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop

    # Every pass runs every tick, and a failing one does not skip the rest.
    assert ran[:6] == ["delivery", "intake", "prune", "delivery", "intake", "prune"]


async def test_the_prune_pass_is_handed_every_live_route_and_the_last_cursor(monkeypatch):
    """The pass reclaims the thread indexes of the routes it is HANDED and no others, so a
    wiring that passed an empty list would stay green while both indexes grew forever. The
    cursor it returns must come back to it, or every pass restarts at the head of the same
    index and the members behind it are never reached."""
    handed: list[tuple[list[str], records_module.PruneCursor]] = []
    stops = [records_module.PruneCursor("beta", 4, 9), records_module.PRUNE_START]

    async def _prune_pass(self, route_names, cursor=records_module.PRUNE_START):
        handed.append((list(route_names), cursor))
        return stops[len(handed) - 1]

    class _Routes:
        async def list_routes(self):
            return {"alpha": object(), "beta": object()}

    monkeypatch.setattr(delivery_module, "get_conversations_manager", _Routes)
    monkeypatch.setattr(records_module.ConversationRecordStore, "prune_expired_terminal_indexes", _prune_pass)
    monkeypatch.setattr(delivery_sweep_module, "_prune_cursor", records_module.PRUNE_START)

    await delivery_sweep_module._prune_terminal_indexes()
    await delivery_sweep_module._prune_terminal_indexes()

    assert [routes for routes, _ in handed] == [["alpha", "beta"], ["alpha", "beta"]]
    # The second pass resumes exactly where the first stopped.
    assert [cursor for _, cursor in handed] == [records_module.PRUNE_START, records_module.PruneCursor("beta", 4, 9)]
    assert delivery_sweep_module._prune_cursor == records_module.PRUNE_START


async def test_starting_the_sweep_twice_cancels_the_first_task(monkeypatch):
    """A second start must cancel the task the first started, or two sweep loops run and
    every recovery pass fires twice a tick."""
    delivery_sweep_module.start_delivery_sweep()
    first = delivery_sweep_module._sweep_task
    assert first is not None
    assert not first.done()

    delivery_sweep_module.start_delivery_sweep()
    second = delivery_sweep_module._sweep_task
    assert second is not None
    assert second is not first
    with pytest.raises(asyncio.CancelledError):
        await first
    assert first.cancelled()

    await delivery_sweep_module.stop_delivery_sweep()
    assert second.cancelled()
    assert delivery_sweep_module._sweep_task is None


async def test_stopping_the_sweep_cancels_and_clears_it(monkeypatch):
    """Shutdown cancels the running sweep and clears the handle; a second stop is a no-op."""
    delivery_sweep_module.start_delivery_sweep()
    task = delivery_sweep_module._sweep_task
    assert task is not None

    await delivery_sweep_module.stop_delivery_sweep()
    assert task.cancelled()
    assert delivery_sweep_module._sweep_task is None
    await delivery_sweep_module.stop_delivery_sweep()  # nothing running: a clean no-op
