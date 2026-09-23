"""Turn scheduling, exactly-once delivery, the delivery race, and intake-lease liveness."""

from __future__ import annotations

import asyncio
import time
import types

import pytest
from tai42_contract.conversations import (
    DeliveryReceipt,
)

from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import accessors as accessors_module

from .conftest import (
    BlockingAgent,
    EchoAgent,
    FakeChannel,
    FakeManager,
    _api_route,
    _channel_route,
    _intake_lease,
    _settle,
    _store,
    _wire,
)
from .fake_record_redis import FakeRecordRedis


async def test_record_delivery_status_confirms_provisional(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_GRACE_SECONDS", "3600")  # keep it provisional
    caps_module._CAPS_CACHE.clear()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    # Let the turn + channel send run, but not the (1h) grace confirm.
    for _ in range(50):
        record = await _store().get_record(message_id)
        if record is not None and record.delivery_status is DeliveryStatus.PROVISIONAL:
            break
        await asyncio.sleep(0.01)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.PROVISIONAL

    # A positive out-of-band receipt confirms it delivered.
    await delivery_module.record_delivery_status("twilio", "out-1", DeliveryReceipt.DELIVERED)
    record = await _store().get_record(message_id)
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED

    # An unknown outbound id is a loud lookup failure.
    with pytest.raises(LookupError):
        await delivery_module.record_delivery_status("twilio", "nope", DeliveryReceipt.DELIVERED)


async def test_redrive_resumes_a_stranded_pending_record(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    # Simulate a record persisted before a crash: pending_delivery, never sent.
    now = time.time()

    record = ConversationRecord(
        message_id="stranded",
        route_name="line",
        door="channel",
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        origin="client",
        inbound_text="ask stranded",
        answer_status="answered",
        answer="resumed",
        created_at=now,
        updated_at=now,
    )
    await _store().create_record(record)

    await delivery_module.redrive_pending()
    await _settle()

    assert channel.sends  # the stranded record was re-driven and sent
    got = await _store().get_record("stranded")
    assert got is not None
    assert got.delivery_status in (DeliveryStatus.PROVISIONAL, DeliveryStatus.DELIVERED)


def _answered_channel_record(message_id: str):
    """A channel record carrying its produced answer — the shape the delivery machine
    picks up, whatever state a test then moves it to."""

    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name="line",
        door="channel",
        thread_id="bridge:line:+15550002222",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        origin="client",
        inbound_text="ask already out",
        answer_status="answered",
        answer="already out",
        created_at=now,
        updated_at=now,
    )


def _set_grace_deadline(fake: FakeRecordRedis, message_id: str, deadline: float) -> None:
    fake._hashes[ConversationsSettings().record_key(message_id)]["grace_deadline"] = str(deadline)


async def test_redrive_confirms_a_provisional_record_whose_grace_elapsed(env, monkeypatch):
    # Provisional with its grace already elapsed: boot confirms it and must NOT re-send
    # an answer the medium already took.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    store = _store()
    await store.create_record(_answered_channel_record("prov-elapsed"))
    await store.mark_provisional("prov-elapsed", ["out-1"], 1, time.time(), "tok")
    _set_grace_deadline(env, "prov-elapsed", time.time() - 1)

    await delivery_module.redrive_pending()
    await _settle()

    record = await store.get_record("prov-elapsed")
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert channel.sends == []


async def test_redrive_reschedules_only_the_remaining_grace_of_a_provisional_record(env, monkeypatch):
    # Still inside its window: the fallback is rebuilt for what is LEFT of the grace,
    # not a fresh full window. Redrive reads a frozen clock so the deadline is
    # deterministically in the future when boot inspects the record, and the rescheduled
    # confirm sleeps only the ~50ms that is left — a full-window reschedule would not
    # confirm within ``_settle``.
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_GRACE_SECONDS", "3600")
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    store = _store()
    now = time.time()
    monkeypatch.setattr(delivery_module, "time", types.SimpleNamespace(time=lambda: now))
    await store.create_record(_answered_channel_record("prov-young"))
    await store.mark_provisional("prov-young", ["out-1"], 1, now, "tok")
    _set_grace_deadline(env, "prov-young", now + 0.05)

    await delivery_module.redrive_pending()
    still_provisional = await store.get_record("prov-young")
    assert still_provisional is not None
    assert still_provisional.delivery_status is DeliveryStatus.PROVISIONAL

    await _settle()

    record = await store.get_record("prov-young")
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert channel.sends == []


async def test_redrive_races_executor_delivers_once(env, monkeypatch):
    _wire(monkeypatch, FakeManager(_api_route()))
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)
    now = time.time()

    record = ConversationRecord(
        message_id="race",
        route_name="chat",
        door="api",
        thread_id="bridge:chat:user-7",
        client_address="user-7",
        callback_url="https://cb.example/x",
        caller_principal="alice",
        origin="client",
        inbound_text="ask once",
        answer_status="answered",
        answer="once",
        created_at=now,
        updated_at=now,
    )
    await _store().create_record(record)

    # Hold BOTH workers at the claim so each reads the record before either writes a
    # lease; the loser must find a live lease and send nothing.
    at_the_claim = asyncio.Barrier(2)
    claim_delivery = records_module.ConversationRecordStore.claim_delivery

    async def _claim_together(self, message_id, now_, token, lease_seconds):
        await asyncio.wait_for(at_the_claim.wait(), 2)
        return await claim_delivery(self, message_id, now_, token, lease_seconds)

    monkeypatch.setattr(records_module.ConversationRecordStore, "claim_delivery", _claim_together)

    # A re-drive and a direct executor race the same record; the atomic claim lets exactly
    # one deliver.
    await asyncio.gather(delivery_module.deliver("race"), delivery_module.deliver("race"))
    await _settle()

    assert len(posted) == 1
    delivered = await _store().get_record("race")
    assert delivered is not None
    assert delivered.delivery_status is DeliveryStatus.DELIVERED


async def test_the_wait_paths_claim_locks_out_a_racing_callback_delivery(env, monkeypatch):
    # The wait path is held between taking its claim and writing the terminal state —
    # the window where only the lease stands between the record and a second delivery.
    _wire(monkeypatch, FakeManager(_api_route()))
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(delivery_module, "_post_callback", _post)
    now = time.time()

    await _store().create_record(
        ConversationRecord(
            message_id="wait-race",
            route_name="chat",
            door="api",
            thread_id="bridge:chat:user-7",
            client_address="user-7",
            callback_url="https://cb.example/x",
            caller_principal="alice",
            origin="client",
            inbound_text="ask once",
            answer_status="answered",
            answer="once",
            created_at=now,
            updated_at=now,
        )
    )

    claimed = asyncio.Event()
    finish = asyncio.Event()
    mark_delivered = records_module.ConversationRecordStore.mark_delivered

    async def _hold_after_the_claim(self, message_id, outbound_ids, attempts, now_, token):
        claimed.set()
        await finish.wait()
        return await mark_delivered(self, message_id, outbound_ids, attempts, now_, token)

    monkeypatch.setattr(records_module.ConversationRecordStore, "mark_delivered", _hold_after_the_claim)

    waiter = asyncio.create_task(delivery_module.mark_wait_delivered("wait-race"))
    await asyncio.wait_for(claimed.wait(), 2)
    held = await _store().get_record("wait-race")
    assert held is not None
    assert held.delivery_status is DeliveryStatus.PENDING_DELIVERY  # nothing terminal yet

    await delivery_module.deliver("wait-race")
    assert posted == []  # the live lease refused the callback

    finish.set()
    assert await waiter is True
    record = await _store().get_record("wait-race")
    assert record is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert posted == []


async def test_a_boot_redrive_leaves_a_live_peers_in_flight_turn_alone(env, monkeypatch):
    # The supported multi-worker shape: a sibling worker boots while this one is mid-turn.
    # The record's intake lease is LIVE, so the re-drive must not terminally fail a message
    # that is being answered correctly.
    agent = BlockingAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await asyncio.wait_for(agent.entered.wait(), 2)

    await turn_module.redrive_accepted()
    await asyncio.sleep(0)

    # Still at intake, and nothing was sent: the live turn was left to the worker running it.
    mid_turn = await store.get_record(message_id)
    assert mid_turn is not None
    assert mid_turn.delivery_status is DeliveryStatus.ACCEPTED
    assert channel.sends == []

    agent.release.set()
    await _settle()

    # ...and the real answer is the one the client gets.
    record = await store.get_record(message_id)
    assert record is not None
    assert record.answer == "echo: hi"
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert [n.message for n in channel.sends] == ["echo: hi"]


async def test_a_running_turn_refreshes_its_intake_lease_past_the_original_expiry(env, monkeypatch):
    # A turn longer than one lease stays live only because it heartbeats: without the
    # refresh its lease lapses and the next boot reaps it mid-flight.
    monkeypatch.setenv("CONVERSATIONS_INTAKE_CLAIM_LEASE_SECONDS", "2")
    monkeypatch.setenv("CONVERSATIONS_INTAKE_CLAIM_REFRESH_SECONDS", "1")
    agent = BlockingAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await asyncio.wait_for(agent.entered.wait(), 2)
    at_accept = await _intake_lease(env, message_id)

    # Past the lease the accept wrote, so only a refresh can still be holding it.
    await asyncio.sleep(2.3)
    assert await _intake_lease(env, message_id) != at_accept

    await turn_module.redrive_accepted()
    await asyncio.sleep(0)
    mid_turn = await store.get_record(message_id)
    assert mid_turn is not None
    assert mid_turn.delivery_status is DeliveryStatus.ACCEPTED
    assert channel.sends == []

    agent.release.set()
    await _settle()
    record = await store.get_record(message_id)
    assert record is not None
    assert record.answer == "echo: hi"
    # The lease is released with the outcome, not left behind on the record.
    assert await _intake_lease(env, message_id) == ""


async def test_a_turn_queued_behind_the_caps_keeps_its_intake_lease_live(env, monkeypatch):
    # A turn waiting on the per-thread FIFO or the global ceiling has not started yet but is
    # every bit as owned as a running one. Its lease must heartbeat while it waits, or a
    # busy worker's own backlog is reaped out from under it and answered with an error.
    monkeypatch.setenv("CONVERSATIONS_INTAKE_CLAIM_LEASE_SECONDS", "2")
    monkeypatch.setenv("CONVERSATIONS_INTAKE_CLAIM_REFRESH_SECONDS", "1")
    agent = BlockingAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    running = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "one", "PID1")
    await asyncio.wait_for(agent.entered.wait(), 2)
    # Same address, so the second message queues behind the first on the thread's FIFO.
    queued = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "two", "PID2")
    assert agent.calls == ["one"]

    # Past the lease the accept wrote: only a heartbeat can still be holding it.
    await asyncio.sleep(2.3)
    await turn_module.redrive_accepted()
    await asyncio.sleep(0)

    for message_id in (running, queued):
        record = await store.get_record(message_id)
        assert record is not None
        assert record.delivery_status is DeliveryStatus.ACCEPTED
    assert channel.sends == []

    agent.release.set()
    await _settle(timeout=5.0)

    assert agent.calls == ["one", "two"]
    assert sorted(n.message for n in channel.sends) == ["echo: one", "echo: two"]
