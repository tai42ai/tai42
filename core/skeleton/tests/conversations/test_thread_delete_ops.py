"""The conversation thread and person delete operations: the absolute, idempotent teardown of
a thread's checkpoint, answer records and indexes (route-keyed and linked-person aggregate),
the in-flight-turn guard, the interrupted-retry finish, and the parked-ask cascade."""

from __future__ import annotations

import time

import pytest

from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.errors import BadRequestError, ConflictError, NotFoundError

from .conftest import _assert_park_cancelled, _seed_park

_THREAD = "bridge:chat:+15550001111"
_PERSON_THREAD = "bridge:@person:p1"


class _RecordingSaver:
    """A checkpoint saver that records every thread ``adelete_thread`` was asked to forget."""

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


@pytest.fixture
def checkpoint_saver(monkeypatch) -> _RecordingSaver:
    """Stub the checkpoint access path the thread delete reaches, capturing the thread ids it
    asks the saver to forget — the agent's actual memory store."""
    import tai42_kit.llm.checkpoint.checkpoint_registry as registry_mod
    import tai42_kit.llm.settings as settings_mod

    saver = _RecordingSaver()
    monkeypatch.setattr(registry_mod, "checkpoint_registry", lambda: _FakeCheckpointRegistry(saver))
    monkeypatch.setattr(settings_mod, "llm_provider_settings", lambda: _FakeProviderSettings())
    return saver


async def _seed_record(*, message_id: str, route_name: str, thread_id: str) -> None:
    """One delivered record under ``route_name`` on ``thread_id`` — a distinct message id per
    call, so a thread spanning several routes gets a distinct record on each."""
    now = time.time()
    await ConversationRecordStore(ConversationsSettings()).create_record(
        ConversationRecord(
            message_id=message_id,
            route_name=route_name,
            door="channel",
            thread_id=thread_id,
            client_address="+15550001111",
            channel="twilio",
            our_identity="+15550001111",
            origin="client",
            inbound_text="ask",
            answer_status="answered",
            answer="the answer",
            delivery_status=DeliveryStatus.DELIVERED,
            created_at=now,
            updated_at=now,
        )
    )


async def _seed_accepted(*, message_id: str, route_name: str, thread_id: str) -> None:
    """One ``accepted`` intake record holding a live intake lease — a turn in flight."""
    now = time.time()
    await ConversationRecordStore(ConversationsSettings()).create_record(
        ConversationRecord(
            message_id=message_id,
            route_name=route_name,
            door="channel",
            thread_id=thread_id,
            client_address="+15550001111",
            channel="twilio",
            our_identity="+15550001111",
            origin="client",
            inbound_text="ask",
            delivery_status=DeliveryStatus.ACCEPTED,
            created_at=now,
            updated_at=now,
        ),
        intake_token="tok",
    )


async def test_delete_thread_clears_checkpoint_records_and_indexes(wired, record_redis, checkpoint_saver):
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="m0", route_name="chat", thread_id=_THREAD)
    settings = ConversationsSettings()
    assert settings.thread_index_key("chat", _THREAD) in record_redis._zsets

    result = await ops.delete_conversation_thread("chat", _THREAD)

    assert result == {"removed": 1, "route_name": "chat", "thread_id": _THREAD}
    # a. the agent checkpoint — adelete_thread asked to forget exactly this thread.
    assert checkpoint_saver.deleted == [_THREAD]
    # b. the answer record itself.
    assert settings.record_key("m0") not in record_redis._hashes
    # c. both thread indexes.
    assert settings.thread_index_key("chat", _THREAD) not in record_redis._zsets
    assert _THREAD not in record_redis._zsets.get(settings.route_threads_key("chat"), {})
    # Forgetting is absolute: a re-run of a now-empty id on its own route succeeds with
    # removed=0 and forgets the checkpoint again, never a 404.
    rerun = await ops.delete_conversation_thread("chat", _THREAD)
    assert rerun == {"removed": 0, "route_name": "chat", "thread_id": _THREAD}
    assert checkpoint_saver.deleted == [_THREAD, _THREAD]


async def test_delete_never_seen_thread_on_its_route_forgets_the_checkpoint_with_removed_zero(
    wired, record_redis, checkpoint_saver
):
    # Forgetting is absolute: an id on its own route that the index never held is not a 404 —
    # it succeeds with removed=0 and its checkpoint is deleted regardless, so an aged-out
    # thread whose records already lapsed is still forgotten in agent memory.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    result = await ops.delete_conversation_thread("chat", "bridge:chat:never")

    assert result == {"removed": 0, "route_name": "chat", "thread_id": "bridge:chat:never"}
    assert checkpoint_saver.deleted == ["bridge:chat:never"]


async def test_delete_thread_rejects_a_foreign_route_prefix_and_forgets_no_checkpoint(
    wired, record_redis, checkpoint_saver
):
    # A route-keyed id must carry the named route's ``bridge:{route_name}:`` prefix. An id of
    # another route (prefix mismatch) is a loud 400 — the guard stopping a delete on one route
    # from wiping another route's memory — and the checkpoint is never touched.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    with pytest.raises(BadRequestError, match="not a thread of route 'chat'"):
        await ops.delete_conversation_thread("chat", "bridge:other:+15550001111")
    assert checkpoint_saver.deleted == []


async def test_delete_thread_with_a_turn_in_flight_is_a_409(wired, record_redis, checkpoint_saver):
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_accepted(message_id="live", route_name="chat", thread_id=_THREAD)
    settings = ConversationsSettings()

    with pytest.raises(ConflictError, match="turn in flight"):
        await ops.delete_conversation_thread("chat", _THREAD)
    # Refused before any teardown: the intake record stands and the checkpoint is untouched.
    assert checkpoint_saver.deleted == []
    assert settings.record_key("live") in record_redis._hashes
    assert settings.thread_index_key("chat", _THREAD) in record_redis._zsets


async def test_delete_thread_waits_behind_an_in_flight_operator_send(wired, record_redis, checkpoint_saver):
    # The thread delete's teardown takes the thread's per-thread FIFO — the SAME lock an
    # operator send and an in-flight turn hold — so a delete cannot interleave an in-flight
    # operator send's checkpoint + record writes: it lands only once the holder releases.
    import asyncio

    from tai42_skeleton.conversations import caps as caps_module

    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="m0", route_name="chat", thread_id=_THREAD)

    caps_module._CAPS_CACHE.clear()
    caps = caps_module.get_turn_caps()
    holding = asyncio.Event()
    release = asyncio.Event()

    async def _hold_the_thread() -> None:
        caps.reserve_thread_slot(_THREAD)
        async with caps.run_reserved(_THREAD):
            holding.set()
            await release.wait()

    holder = asyncio.create_task(_hold_the_thread())
    await asyncio.wait_for(holding.wait(), 1.0)

    delete = asyncio.create_task(ops.delete_conversation_thread("chat", _THREAD))
    # While the holder keeps the thread, the delete is blocked before any teardown: the
    # checkpoint is untouched.
    await asyncio.sleep(0.05)
    assert not delete.done()
    assert checkpoint_saver.deleted == []

    # The holder releases the thread; only now does the delete's teardown proceed.
    release.set()
    await holder
    result = await delete

    assert result == {"removed": 1, "route_name": "chat", "thread_id": _THREAD}
    assert checkpoint_saver.deleted == [_THREAD]


async def test_delete_thread_rechecks_the_live_intake_under_the_lock(wired, record_redis, checkpoint_saver):
    # The in-flight guard is checked AGAIN under the FIFO: an intake admitted after the cheap
    # outer check but before the delete holds the lock is caught in-lock and 409s, so its turn
    # — queued behind the delete's lock — never runs after the teardown and re-creates the
    # checkpoint. Deterministic simulation: a holder pins the lock, the delete passes the outer
    # check and blocks on it, an accepted intake is seeded while it waits, then the holder
    # releases and the delete's in-lock re-check finds the intake.
    import asyncio

    from tai42_skeleton.conversations import caps as caps_module

    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="m0", route_name="chat", thread_id=_THREAD)

    caps_module._CAPS_CACHE.clear()
    caps = caps_module.get_turn_caps()
    holding = asyncio.Event()
    release = asyncio.Event()

    async def _hold_the_thread() -> None:
        caps.reserve_thread_slot(_THREAD)
        async with caps.run_reserved(_THREAD):
            holding.set()
            await release.wait()

    holder = asyncio.create_task(_hold_the_thread())
    await asyncio.wait_for(holding.wait(), 1.0)

    delete = asyncio.create_task(ops.delete_conversation_thread("chat", _THREAD))
    # The delete clears the outer check (no live intake yet) and blocks on the held lock: it is
    # alive and has torn nothing down.
    await asyncio.sleep(0.05)
    assert not delete.done()
    assert checkpoint_saver.deleted == []

    # A turn is admitted while the delete waits behind the lock.
    await _seed_accepted(message_id="slipped-in", route_name="chat", thread_id=_THREAD)

    release.set()
    await holder
    # The delete acquires the lock, re-checks, and refuses — from within the lock, never a
    # teardown that would strand the admitted turn's memory half-forgotten.
    with pytest.raises(ConflictError, match="turn in flight"):
        await delete
    assert checkpoint_saver.deleted == []
    settings = ConversationsSettings()
    assert settings.record_key("m0") in record_redis._hashes
    assert settings.thread_index_key("chat", _THREAD) in record_redis._zsets


async def test_delete_thread_ignores_a_live_intake_on_another_thread(wired, record_redis, checkpoint_saver):
    # An accepted record holding a LIVE lease on a DIFFERENT thread is not a turn in flight on
    # this one, so the delete proceeds.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="m0", route_name="chat", thread_id=_THREAD)
    await _seed_accepted(message_id="elsewhere", route_name="chat", thread_id="bridge:chat:+15559990000")

    result = await ops.delete_conversation_thread("chat", _THREAD)
    assert result["removed"] == 1
    assert checkpoint_saver.deleted == [_THREAD]


async def test_delete_thread_proceeds_when_the_target_thread_intake_lease_has_lapsed(
    wired, record_redis, checkpoint_saver
):
    # An accepted record ON THE TARGET THREAD whose intake lease has LAPSED is not a turn in
    # flight: its worker is gone, so the delete proceeds and forgets the memory. Pins the
    # ``expiry > now`` liveness comparison — inverting it would 409 this dead lease.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_accepted(message_id="lapsed", route_name="chat", thread_id=_THREAD)
    settings = ConversationsSettings()
    # Force the stored lease expiry into the past — the way records.py reads it is
    # ``float(expiry) > now``, so a past expiry is a dead lease.
    record_redis._hashes[settings.record_key("lapsed")]["intake_claim"] = f"tok:{time.time() - 3600}"

    result = await ops.delete_conversation_thread("chat", _THREAD)

    assert result == {"removed": 1, "route_name": "chat", "thread_id": _THREAD}
    assert checkpoint_saver.deleted == [_THREAD]


async def test_delete_person_thread_clears_every_route_index(wired, record_redis, checkpoint_saver, monkeypatch):
    from datetime import UTC, datetime

    from tai42_contract.conversations import Person, PersonAddress

    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="relay",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel",
                routes=["chat-a"],
                channel="twilio",
                our_identity="+1",
                address="+1000",
                linked_at=datetime.now(UTC),
            ),
            PersonAddress(
                door="channel",
                routes=["chat-b"],
                channel="whatsapp",
                our_identity="+2",
                address="+2000",
                linked_at=datetime.now(UTC),
            ),
        ],
    )

    class _FakePersonStore:
        async def get_by_id(self, person_id: str) -> Person | None:
            return person if person_id == person.person_id else None

    monkeypatch.setattr(ops, "_person_store", lambda: _FakePersonStore())

    settings = ConversationsSettings()
    record_redis.seed_route("chat-a")
    record_redis.seed_route("chat-b")
    await _seed_record(message_id="pa", route_name="chat-a", thread_id=_PERSON_THREAD)
    await _seed_record(message_id="pb", route_name="chat-b", thread_id=_PERSON_THREAD)

    # The supplied route governs authz only; the delete spans every route the person wrote under.
    result = await ops.delete_conversation_thread("chat-a", _PERSON_THREAD)

    assert result == {"removed": 2, "route_name": "chat-a", "thread_id": _PERSON_THREAD}
    # One aggregated checkpoint thread, forgotten once.
    assert checkpoint_saver.deleted == [_PERSON_THREAD]
    # Every route index that carried the thread is reclaimed — else a member strands under one.
    for route_name in ("chat-a", "chat-b"):
        assert settings.thread_index_key(route_name, _PERSON_THREAD) not in record_redis._zsets
        assert _PERSON_THREAD not in record_redis._zsets.get(settings.route_threads_key(route_name), {})


async def test_delete_person_thread_404s_when_the_route_is_not_the_persons(
    wired, record_redis, checkpoint_saver, monkeypatch
):
    from datetime import UTC, datetime

    from tai42_contract.conversations import Person, PersonAddress

    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="relay",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel",
                routes=["chat-a"],
                channel="twilio",
                our_identity="+1",
                address="+1",
                linked_at=datetime.now(UTC),
            )
        ],
    )

    class _FakePersonStore:
        async def get_by_id(self, person_id: str) -> Person | None:
            return person

    monkeypatch.setattr(ops, "_person_store", lambda: _FakePersonStore())

    with pytest.raises(NotFoundError):
        await ops.delete_conversation_thread("chat-b", _PERSON_THREAD)
    assert checkpoint_saver.deleted == []


async def test_delete_person_thread_with_a_turn_in_flight_is_a_409(wired, record_redis, checkpoint_saver, monkeypatch):
    # The in-flight guard reads the aggregated ``bridge:@person:{id}`` key an accepted record
    # carries, so a turn running on any of the person's routes refuses the delete before any
    # teardown — else its completion re-stamps a route index behind the drop.
    from datetime import UTC, datetime

    from tai42_contract.conversations import Person, PersonAddress

    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="relay",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel",
                routes=["chat-a"],
                channel="twilio",
                our_identity="+1",
                address="+1000",
                linked_at=datetime.now(UTC),
            ),
            PersonAddress(
                door="channel",
                routes=["chat-b"],
                channel="whatsapp",
                our_identity="+2",
                address="+2000",
                linked_at=datetime.now(UTC),
            ),
        ],
    )

    class _FakePersonStore:
        async def get_by_id(self, person_id: str) -> Person | None:
            return person if person_id == person.person_id else None

    monkeypatch.setattr(ops, "_person_store", lambda: _FakePersonStore())

    settings = ConversationsSettings()
    record_redis.seed_route("chat-a")
    record_redis.seed_route("chat-b")
    await _seed_accepted(message_id="plive", route_name="chat-a", thread_id=_PERSON_THREAD)

    with pytest.raises(ConflictError, match="turn in flight"):
        await ops.delete_conversation_thread("chat-a", _PERSON_THREAD)
    # Refused before any teardown: the intake record stands, the checkpoint is untouched.
    assert checkpoint_saver.deleted == []
    assert settings.record_key("plive") in record_redis._hashes
    assert settings.thread_index_key("chat-a", _PERSON_THREAD) in record_redis._zsets


async def test_an_interrupted_thread_delete_is_finished_by_a_retry_not_404d(
    wired, record_redis, checkpoint_saver, monkeypatch
):
    # The reclamation runs after the checkpoint delete, so a redis blip mid-drop strands the
    # records/indexes it had not reached. A retry re-runs it and FINISHES, never a 404 that
    # would leave those keys stranded (neither thread index carries a TTL).
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="r0", route_name="chat", thread_id=_THREAD)
    await _seed_record(message_id="r1", route_name="chat", thread_id=_THREAD)
    settings = ConversationsSettings()

    inner_eval = record_redis.eval
    state = {"blown": False}

    async def _eval_blows_once(script, numkeys, *args):
        if "conversations:record:delete" in script and not state["blown"]:
            state["blown"] = True
            raise TimeoutError("redis blip mid-drop")
        return await inner_eval(script, numkeys, *args)

    monkeypatch.setattr(record_redis, "eval", _eval_blows_once)

    with pytest.raises(TimeoutError):
        await ops.delete_conversation_thread("chat", _THREAD)
    # The checkpoint went first, and the thread index survives holding the un-reclaimed work.
    assert checkpoint_saver.deleted == [_THREAD]
    assert settings.thread_index_key("chat", _THREAD) in record_redis._zsets

    result = await ops.delete_conversation_thread("chat", _THREAD)

    # Not a 404: the retry finished the reclamation.
    assert result["removed"] == 2
    assert result["thread_id"] == _THREAD
    assert settings.thread_index_key("chat", _THREAD) not in record_redis._zsets
    # The checkpoint delete is idempotent under the re-run.
    assert checkpoint_saver.deleted == [_THREAD, _THREAD]


async def test_delete_thread_rejects_a_blank_thread_id(wired):
    with pytest.raises(BadRequestError, match="thread_id"):
        await ops.delete_conversation_thread("chat", "   ")


async def test_delete_thread_rejects_a_bad_route_slug(wired):
    with pytest.raises(BadRequestError, match="route_name"):
        await ops.delete_conversation_thread("Chat Room", _THREAD)


# -- the parked-ask cascade (parked-ask orphaned by thread/person deletion) --


async def test_delete_thread_cascade_cancels_a_parked_ask(wired, record_redis, checkpoint_saver, interactions_parks):
    store, fake = interactions_parks
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="m0", route_name="chat", thread_id=_THREAD)
    await _seed_park(store, fake, interaction_id="i1", group_id="g1", thread_id=_THREAD)
    # A park on an UNRELATED thread must survive the delete untouched.
    other_thread = "bridge:chat:+15559990000"
    await _seed_park(store, fake, interaction_id="keep", group_id="kg", thread_id=other_thread)

    result = await ops.delete_conversation_thread("chat", _THREAD)

    assert result == {"removed": 1, "route_name": "chat", "thread_id": _THREAD}
    await _assert_park_cancelled(store, fake, interaction_id="i1", thread_id=_THREAD)
    # The unrelated thread's park is fully intact.
    assert await store.get_state(fake, "keep") is not None
    assert await fake.smembers(store.thread_parks_key(other_thread)) == {"keep"}


async def test_delete_thread_cascade_is_idempotent_on_rerun(wired, record_redis, checkpoint_saver, interactions_parks):
    store, fake = interactions_parks
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_park(store, fake, interaction_id="i1", group_id="g1", thread_id=_THREAD)

    await ops.delete_conversation_thread("chat", _THREAD)
    # A retry of the absolute-forget delete re-runs the cascade cleanly (a no-op second time).
    rerun = await ops.delete_conversation_thread("chat", _THREAD)
    assert rerun == {"removed": 0, "route_name": "chat", "thread_id": _THREAD}
    await _assert_park_cancelled(store, fake, interaction_id="i1", thread_id=_THREAD)


async def test_delete_person_cascade_cancels_a_parked_ask(
    wired, record_redis, checkpoint_saver, interactions_parks, monkeypatch
):
    from datetime import UTC, datetime

    from tai42_contract.conversations import Person, PersonAddress

    store, fake = interactions_parks
    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="relay",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel",
                routes=["chat-a"],
                channel="twilio",
                our_identity="+1",
                address="+1000",
                linked_at=datetime.now(UTC),
            ),
        ],
    )

    class _FakePersonStore:
        async def get_by_id(self, person_id: str) -> Person | None:
            return person if person_id == person.person_id else None

        async def erase(self, p: Person):
            return True, 1

    monkeypatch.setattr(ops, "_person_store", lambda: _FakePersonStore())
    record_redis.seed_route("chat-a")
    await _seed_record(message_id="pa", route_name="chat-a", thread_id=_PERSON_THREAD)
    await _seed_park(store, fake, interaction_id="i1", group_id="g1", thread_id=_PERSON_THREAD)

    result = await ops.delete_conversation_person("p1")

    assert result == {"person_id": "p1", "removed": 1, "erased": True}
    await _assert_park_cancelled(store, fake, interaction_id="i1", thread_id=_PERSON_THREAD)
