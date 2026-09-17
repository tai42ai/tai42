"""Intake commit-recovery and the re-drive of a record stranded mid-turn (message and event families)."""

from __future__ import annotations

import asyncio
import time

import pytest

from tai42_skeleton.conversations import caps as caps_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations import turn as turn_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.turn import accessors as accessors_module
from tai42_skeleton.conversations.turn import intake as intake_module
from tai42_skeleton.conversations.turn import outcome as outcome_module
from tai42_skeleton.conversations.turn import target as target_module

from .conftest import (
    BlockingAgent,
    EchoAgent,
    FakeChannel,
    FakeManager,
    _all_record_ids,
    _channel_route,
    _create_stranded_intake,
    _intake_record,
    _settle,
    _store,
    _tool_channel_route,
    _wire,
)
from .fake_record_redis import FakeRecordRedis


async def test_thread_overflow_writes_no_state_so_the_provider_retry_succeeds(env, monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_THREAD_QUEUE_DEPTH", "1")
    caps_module._CAPS_CACHE.clear()
    agent = BlockingAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    first = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "one", "PID1")
    await asyncio.wait_for(agent.entered.wait(), 2)

    # The thread's only slot is taken, so the next message is refused LOUDLY...
    with pytest.raises(caps_module.ThreadQueueOverflowError):
        await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "two", "PID2")
    # ...with ZERO state written: the dedupe pair is unclaimed, so the refusal is
    # honestly retriable.
    assert await store.get_inbound_owner("twilio", "PID2") is None
    assert await _all_record_ids(store) == [first]

    agent.release.set()
    await _settle()

    # The retry after the thread drains is a genuinely fresh attempt, not a dedupe hit
    # on a message that never ran.
    retry = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "two", "PID2")
    await _settle()

    assert retry != first
    assert agent.calls == ["one", "two"]
    record = await store.get_record(retry)
    assert record is not None
    assert record.answer == "echo: two"
    assert record.delivery_status is DeliveryStatus.DELIVERED


async def test_a_failed_persist_leaves_the_inbound_pair_unclaimed(env, monkeypatch):
    # The record is persisted BEFORE the claim on every accept path, so a store failure
    # cannot burn the 48h idempotency slot on a message with nothing behind it.
    monkeypatch.setenv("CONVERSATIONS_PER_ADDRESS_TURNS_PER_HOUR", "1")
    caps_module._CAPS_CACHE.clear()
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})

    async def _boom(self, record, **kwargs):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(records_module.ConversationRecordStore, "create_record", _boom)
    store = _store()

    # One accept per admission verdict: admitted, shed with a paid reply, shed silently.
    for provider_message_id in ("PID1", "PID2", "PID3"):
        with pytest.raises(RuntimeError, match="redis is down"):
            await turn_module.accept(
                "twilio", "+15550001111", "+15550002222", "+15550002222", "hi", provider_message_id
            )
        assert await store.get_inbound_owner("twilio", provider_message_id) is None
    # The admitted attempt also gave its FIFO reservation back rather than leaking it.
    assert caps_module.get_turn_caps()._thread_waiters == {}


async def test_an_indeterminate_inbound_claim_is_resolved_not_left_at_intake(env, monkeypatch):
    # The commit point: the claim's EVAL is APPLIED and only its reply is lost. The pair is
    # now committed to this record, so the provider's redelivery dedupes to it forever —
    # the accept must resolve it before it re-raises, never leave it stranded at intake.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    store = _store()
    claim_inbound = records_module.ConversationRecordStore.claim_inbound

    lost: list[str] = []

    async def _claim_then_lose_the_reply(self, channel_name, provider_message_id, message_id):
        owner = await claim_inbound(self, channel_name, provider_message_id, message_id)
        if not lost:
            # Only the commit-point call loses its reply; the resolution's own arbitration
            # reaches the same claim and reads back what landed.
            lost.append(message_id)
            raise TimeoutError("the reply never came back")
        return owner

    monkeypatch.setattr(records_module.ConversationRecordStore, "claim_inbound", _claim_then_lose_the_reply)

    with pytest.raises(TimeoutError):
        await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID-lost")
    await _settle()

    owner = await store.get_inbound_owner("twilio", "PID-lost")
    assert owner is not None
    record = await store.get_record(owner)
    assert record is not None
    # Resolved with the client-safe error outcome and delivered, not left ``accepted``.
    assert record.delivery_status is not DeliveryStatus.ACCEPTED
    assert record.answer_status == "error"
    assert [send.message for send in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]
    # And the FIFO slot the accept reserved was given back.
    assert caps_module.get_turn_caps()._thread_waiters == {}


async def test_a_racing_duplicate_accept_leaves_one_record_and_releases_its_slot(env, monkeypatch):
    # Two accepts of the SAME provider message id that both got past the redelivery
    # fast-path read: the atomic claim arbitrates them, and the loser owns nothing.
    agent = EchoAgent()
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()
    caps = caps_module.get_turn_caps()
    thread_id = "bridge:line:+15550002222"

    async def _attempt(message_id: str) -> str:
        return await intake_module._accept_for_turn(
            store,
            route=_channel_route(),
            channel="twilio",
            message_id=message_id,
            thread_id=thread_id,
            client_address="+15550002222",
            text="hi",
            provider_message_id="PID1",
        )

    winner = await _attempt("winner")
    loser = await _attempt("loser")

    assert winner == "winner"
    assert loser == "winner"  # the loser answers with the id the pair is committed to
    assert await _all_record_ids(store) == ["winner"]
    # Only the winner's reservation stands; the loser handed its slot straight back.
    assert caps._thread_waiters[thread_id] == 1

    await _settle()

    assert thread_id not in caps._thread_waiters
    assert len(agent.calls) == 1


async def test_a_settings_reload_mid_accept_does_not_leak_the_reserved_slot(env, monkeypatch):
    # A reload rebuilds the caps, and the turn must keep running against the instance its
    # slot was reserved on: released on any other one, the reservation would sit on the old
    # instance forever and permanently narrow the thread's FIFO.
    agent = EchoAgent()
    _wire(monkeypatch, FakeManager(_channel_route()), FakeChannel())
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    reserving_caps = caps_module.get_turn_caps()

    create_record = records_module.ConversationRecordStore.create_record

    async def _reload_then_create(self, record, **kwargs):
        # The reload lands after the reservation and before the turn is scheduled.
        caps_module._CAPS_CACHE.clear()
        await create_record(self, record, **kwargs)

    monkeypatch.setattr(records_module.ConversationRecordStore, "create_record", _reload_then_create)

    await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    assert agent.calls == [("hi", "bridge:line:+15550002222")]
    assert reserving_caps is not caps_module.get_turn_caps()
    assert reserving_caps._thread_waiters == {}


async def test_redrive_claims_and_adopts_a_record_stranded_before_its_claim(env, monkeypatch):
    # Crash between persisting the intake record and claiming the pair: nobody owns the
    # pair, so the re-drive claims it on the record's behalf and adopts the record.
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()
    await _create_stranded_intake(store, env, "stranded")

    await turn_module.redrive_accepted()
    await _settle()

    assert await store.get_inbound_owner("twilio", "PID1") == "stranded"
    record = await store.get_record("stranded")
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert channel.sends  # the error outcome reached the client
    # The turn is NOT re-run: it dispatches authorized tools under a real execution key,
    # so re-running it could repeat a real-world side effect.
    assert agent.calls == []


async def test_redrive_of_a_record_stranded_mid_turn_fails_it_and_never_reruns_the_turn(env, monkeypatch):
    # Crash mid-turn: the intake lease has lapsed and the record already owns its claim, so
    # the re-drive adopts it and terminally fails it rather than running the agent again.
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()
    await _create_stranded_intake(store, env, "stranded")
    assert await store.claim_inbound("twilio", "PID1", "stranded") == "stranded"

    await turn_module.redrive_accepted()
    await _settle()

    record = await store.get_record("stranded")
    assert record is not None
    assert record.answer_status == "error"
    assert record.error is not None
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]
    assert agent.calls == []


async def test_a_stranded_turn_uses_the_route_error_reply_text_when_set(env, monkeypatch):
    # The stranded-turn repair resolves the record's route best-effort, so the participant sees the
    # route's own ``error_reply_text`` — the same custom reply a live failed turn would send.
    spanish = "Lo sentimos, algo salió mal. Inténtalo de nuevo."
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route(error_reply_text=spanish)), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()
    await _create_stranded_intake(store, env, "stranded")
    assert await store.claim_inbound("twilio", "PID1", "stranded") == "stranded"

    await turn_module.redrive_accepted()
    await _settle()

    record = await store.get_record("stranded")
    assert record is not None
    assert record.answer_status == "error"
    # The participant sees the route's custom reply; the internal detail keeps the built-in wording.
    assert record.answer == spanish
    assert record.error is not None
    assert [n.message for n in channel.sends] == [spanish]
    assert agent.calls == []


async def test_a_stranded_turn_falls_back_to_the_default_when_the_route_lookup_fails(env, monkeypatch):
    # A route lookup that fails (manager unavailable, route gone, exception) must never make
    # this repair path less robust: it falls back to the built-in default reply.
    agent = EchoAgent()
    channel = FakeChannel()
    manager = FakeManager(_channel_route())
    _wire(monkeypatch, manager, channel)

    async def _boom(name: str):
        raise RuntimeError("route store unavailable")

    monkeypatch.setattr(manager, "get_route", _boom)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()
    await _create_stranded_intake(store, env, "stranded")
    assert await store.claim_inbound("twilio", "PID1", "stranded") == "stranded"

    await turn_module.redrive_accepted()
    await _settle()

    record = await store.get_record("stranded")
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]
    assert agent.calls == []


async def test_two_re_drives_adopting_one_stranded_record_still_produce_one_outcome(env, monkeypatch):
    # The lease gate is not the exactly-once guard. With both workers adopting the same
    # record, the guarded complete_turn transition still admits ONE outcome and the client
    # is sent ONE message.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    store = _store()
    await _create_stranded_intake(store, env, "stranded")
    assert await store.claim_inbound("twilio", "PID1", "stranded") == "stranded"

    async def _both_adopt(self, message_id, now, token, lease_seconds):
        return 1

    monkeypatch.setattr(records_module.ConversationRecordStore, "claim_intake", _both_adopt)

    await asyncio.gather(turn_module.redrive_accepted(), turn_module.redrive_accepted())
    await _settle()

    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]
    resolved = await store.get_record("stranded")
    assert resolved is not None
    assert resolved.delivery_status is DeliveryStatus.DELIVERED


async def test_a_turn_task_that_dies_resolves_its_own_record_without_waiting_for_a_boot(env, monkeypatch):
    # The worker is still alive and has just watched its own turn task fail, so it knows
    # the turn is over: the record is given its error outcome and delivered right there,
    # not held at intake until whenever the process next restarts.
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})

    async def _die(**kwargs):
        raise RuntimeError("the record store went away mid-turn")

    monkeypatch.setattr(target_module, "_complete_turn", _die)
    store = _store()

    message_id = await turn_module.accept("twilio", "+15550001111", "+15550002222", "+15550002222", "hi", "PID1")
    await _settle()

    record = await store.get_record(message_id)
    assert record is not None
    assert record.answer_status == "error"
    assert record.answer == outcome_module._ERROR_ANSWER_TEXT
    assert record.delivery_status is DeliveryStatus.DELIVERED
    assert [n.message for n in channel.sends] == [outcome_module._ERROR_ANSWER_TEXT]
    # The thread's FIFO slot is given back too, so the failure costs the thread nothing.
    assert caps_module.get_turn_caps()._thread_waiters == {}


async def test_redrive_discards_an_intake_record_that_lost_its_claim(env, monkeypatch):
    # The pair is owned by another attempt, so this record is a stranded loser: it is
    # discarded, and nothing is delivered for it.
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": EchoAgent()})
    store = _store()
    await _create_stranded_intake(store, env, "loser")
    assert await store.claim_inbound("twilio", "PID1", "winner") == "winner"

    await turn_module.redrive_accepted()
    await _settle()

    assert await store.get_record("loser") is None
    assert channel.sends == []


async def test_redrive_isolates_a_failing_record_and_resolves_the_rest(env, monkeypatch):
    # One record raising must not abandon every other stranded record in the pass, nor abort
    # the boot handler that runs it: the failing record is left for the next pass.
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()

    async def _strand(message_id: str, provider_message_id: str) -> None:
        await store.create_record(_intake_record(message_id, provider_message_id), intake_token="dead-worker")
        key = ConversationsSettings().record_key(message_id)
        env.seed_hash(key, (await env.hgetall(key)) | {"intake_claim": f"dead-worker:{time.time() - 1}"})

    # list_by_status orders by member, so "bad" is reached before "good".
    await _strand("bad", "PID1")
    await _strand("good", "PID2")

    real_claim = records_module.ConversationRecordStore.claim_intake

    async def _claim(self, message_id, *args, **kwargs):
        if message_id == "bad":
            raise RuntimeError("a redis blip on this one record")
        return await real_claim(self, message_id, *args, **kwargs)

    monkeypatch.setattr(records_module.ConversationRecordStore, "claim_intake", _claim)

    await turn_module.redrive_accepted()
    await _settle()

    left = await store.get_record("bad")
    assert left is not None
    assert left.delivery_status is DeliveryStatus.ACCEPTED
    resolved = await store.get_record("good")
    assert resolved is not None
    assert resolved.answer_status == "error"
    assert resolved.delivery_status is DeliveryStatus.DELIVERED
    assert agent.calls == []


def _accepted_event_record(message_id: str, event_id: str = "evt-1") -> ConversationRecord:
    from tai42_skeleton.conversations.models import ConversationRecord

    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name="tool-line",
        door="channel",
        thread_id="bridge:tool-line:+15550002222",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        origin="client",
        inbound_text="",
        inbound_kind="event",
        inbound_event={"event_id": event_id, "kind": "provider.update", "payload": {}},
        submitted_by="svc-int",
        delivery_status=DeliveryStatus.ACCEPTED,
        created_at=now,
        updated_at=now,
    )


async def _strand_event_record(store: ConversationRecordStore, fake: FakeRecordRedis, record) -> None:
    await store.create_record(record, intake_token="dead-worker")
    key = ConversationsSettings().record_key(record.message_id)
    fields = await fake.hgetall(key)
    fake.seed_hash(key, fields | {"intake_claim": f"dead-worker:{time.time() - 1}"})


async def test_redrive_redrives_a_stranded_event_record_that_owns_its_claim(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    store = _store()
    record = _accepted_event_record("stranded-event")
    await _strand_event_record(store, env, record)
    # The record owns its event claim (a crashed worker that claimed before dying).
    assert await store.claim_event("tool-line", "evt-1", "stranded-event") == "stranded-event"

    await turn_module.redrive_accepted()
    await _settle()

    resolved = await store.get_record("stranded-event")
    assert resolved is not None
    assert resolved.answer_status == "error"
    assert resolved.delivery_status is DeliveryStatus.DELIVERED
    assert channel.sends  # the error outcome reached the thread's address


async def test_redrive_discards_a_stranded_event_record_whose_claim_belongs_elsewhere(env, monkeypatch):
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_tool_channel_route()), channel)
    store = _store()
    record = _accepted_event_record("loser-event")
    await _strand_event_record(store, env, record)
    # Another record already owns the event claim, so the stranded one lost it.
    assert await store.claim_event("tool-line", "evt-1", "winner-event") == "winner-event"

    await turn_module.redrive_accepted()
    await _settle()

    assert await store.get_record("loser-event") is None  # discarded, not delivered
    assert channel.sends == []


@pytest.mark.parametrize("transition", ["merge_record", "supersede_record"])
async def test_an_overlap_terminal_record_is_never_redriven_or_stranded(env, monkeypatch, transition):
    # A merged/superseded record has LEFT ``accepted``, so the intake re-drive (which adopts only
    # lapsed ``accepted`` records) never sees it as stranded, never re-runs its turn and never
    # fails it — the overlap outcome stands, and nothing is sent.
    agent = EchoAgent()
    channel = FakeChannel()
    _wire(monkeypatch, FakeManager(_channel_route()), channel)
    monkeypatch.setattr(accessors_module, "_agent_registry", lambda: {"echo": agent})
    store = _store()
    await _create_stranded_intake(store, env, "stranded")
    status = DeliveryStatus.MERGED if transition == "merge_record" else DeliveryStatus.SUPERSEDED
    accepted = await store.get_record("stranded")
    assert accepted is not None
    terminal = ConversationRecord.model_validate(
        accepted.model_dump()
        | {"delivery_status": status.value, "answer_status": None, "answer": None, "successor_id": "lead"}
    )
    assert await getattr(store, transition)(terminal) == 1

    await turn_module.redrive_accepted()
    await _settle()

    record = await store.get_record("stranded")
    assert record is not None
    assert record.delivery_status is status
    assert record.successor_id == "lead"
    assert record.answer_status is None
    assert agent.calls == []
    assert channel.sends == []
    # Neither scan the re-drive and the delivery sweep read ever names it.
    assert await store.list_by_status(frozenset({DeliveryStatus.ACCEPTED})) == []
    assert [work.message_id for work in await store.pending_work()] == []
