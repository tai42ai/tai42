"""The conversation thread/person forget now erases the subject's declared-state records.

The REAL states service, over the in-memory fake Postgres, wired into the delete doors: a thread
reset clears the thread-subject record a turn wrote (RED before the doors were widened — the
record survived the forget), a person erase clears BOTH the person-subject (its cross-thread
memory) and the aggregated thread-subject memory, and a record under a different subject or
target is left untouched. The ``states_erased`` count the door returns is the records forgotten.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from tai42_contract.conversations import Person, PersonAddress
from tai42_contract.states.models import StateBatchWrite, StateSubject, WriteOrigin

import tai42_skeleton.states.store as states_store_module
from tai42_skeleton.app import instance
from tai42_skeleton.operations import conversations as ops

from .test_thread_delete_ops import _PERSON_THREAD, _THREAD, _FakeCheckpointRegistry, _RecordingSaver, _seed_record

_STATE = "assistant_memory"


@pytest.fixture
def checkpoint_saver(monkeypatch) -> _RecordingSaver:
    """Stub the checkpoint access path so the forget's memory delete is captured, not dialled."""
    import tai42_kit.llm.checkpoint.checkpoint_registry as registry_mod
    import tai42_kit.llm.settings as settings_mod

    class _FakeProviderSettings:
        checkpoint = "memory"
        checkpoint_conn_string = None

    saver = _RecordingSaver()
    monkeypatch.setattr(registry_mod, "checkpoint_registry", lambda: _FakeCheckpointRegistry(saver))
    monkeypatch.setattr(settings_mod, "llm_provider_settings", lambda: _FakeProviderSettings())
    return saver


@pytest.fixture
def states_pg(monkeypatch):
    """Wire the states store to an in-memory fake Postgres and mark the feature configured, so the
    delete doors' state-erase step runs the real service SQL rather than no-opping."""
    from ..states.conftest import FakeStatesPg, _FakePool

    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "test")
    fake = FakeStatesPg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        yield _FakePool(fake)

    monkeypatch.setattr(states_store_module, "client_ctx", fake_client_ctx)
    return fake


async def test_thread_delete_erases_the_thread_subject_state(wired, record_redis, checkpoint_saver, states_pg):
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="m0", route_name="chat", thread_id=_THREAD)
    states_pg.seed_declaration(_STATE, subject_kinds=["thread"])
    states_pg.seed_record(_STATE, "agent", "relay", "thread", _THREAD, {"turns": 3})
    # A different thread's state on the same route target must survive the reset.
    states_pg.seed_record(_STATE, "agent", "relay", "thread", "bridge:chat:keep", {"turns": 9})

    result = await ops.delete_conversation_thread("chat", _THREAD)

    assert result["states_erased"] == 1
    assert ("assistant_memory", "agent", "relay", "thread", _THREAD) not in states_pg.records
    assert ("assistant_memory", "agent", "relay", "thread", "bridge:chat:keep") in states_pg.records
    # One RTBF audit row for the forgotten thread; paths [[]] (the whole document).
    assert [w["subject_key"] for w in states_pg.writes] == [_THREAD]
    assert states_pg.writes[0]["paths"] == [[]]


async def test_thread_delete_leaves_person_subject_state_untouched(wired, record_redis, checkpoint_saver, states_pg):
    # A single-thread reset clears only the thread's OWN (thread-subject) memory; state a flow
    # scoped to the person (cross-thread memory) is the "Erase person" door's to clear.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="m0", route_name="chat", thread_id=_THREAD)
    states_pg.seed_declaration(_STATE, subject_kinds=["thread", "person"])
    states_pg.seed_record(_STATE, "agent", "relay", "thread", _THREAD, {"turns": 3})
    states_pg.seed_record(_STATE, "agent", "relay", "person", "p1", {"prefs": 1})

    result = await ops.delete_conversation_thread("chat", _THREAD)

    assert result["states_erased"] == 1
    assert ("assistant_memory", "agent", "relay", "person", "p1") in states_pg.records


def _pending_save(target_name: str, subject_kind: str, subject_key: str) -> StateBatchWrite:
    """A single write-before-reply item for a subject of the ``relay`` target under ``_STATE``."""
    return StateBatchWrite(
        state=_STATE,
        subject=StateSubject(target_kind="agent", target_name=target_name, kind=subject_kind, key=subject_key),
        ops=[{"op": "set", "path": ["n"], "value": 3}],
        origin=WriteOrigin(consumer="turn"),
    )


async def _enqueue_unapplied(monkeypatch, item: StateBatchWrite) -> None:
    """Enqueue ``item`` as a pending save but STOP before it applies — the real window between a
    turn's write-before-reply and the apply-after, where the record has not yet landed."""
    import tai42_skeleton.states.outbox.enqueue as enqueue_mod

    async def _no_dispatch(service, row_id, *, has_records, has_calls):
        return None

    monkeypatch.setattr(enqueue_mod, "dispatch_pending_save", _no_dispatch)
    await instance.app.states.enqueue_batch([item])


async def test_thread_delete_drains_and_erases_a_pending_only_thread_subject_state(
    wired, record_redis, checkpoint_saver, states_pg, monkeypatch
):
    # A turn's thread-subject state write is still a PENDING outbox save (written-before-reply,
    # applied-after) when the reset runs: no record has landed and no alias exists, so the erase's
    # landed-row discovery cannot see it. The reset must DRAIN it first (landing the record) and
    # then forget it — else it later applies and the conversation remembers what the reset erased.
    await ops.create_conversation_route(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="relay",
        execution_key="svc",
        callback_url="https://example.com/cb",
    )
    await _seed_record(message_id="m0", route_name="chat", thread_id=_THREAD)
    states_pg.seed_declaration(_STATE, subject_kinds=["thread"])
    await _enqueue_unapplied(monkeypatch, _pending_save("relay", "thread", _THREAD))
    # Nothing has landed yet: the write lives only as a pending save.
    assert ("assistant_memory", "agent", "relay", "thread", _THREAD) not in states_pg.records
    assert any(o["status"] == "pending" for o in states_pg.outbox.values())

    result = await ops.delete_conversation_thread("chat", _THREAD)

    assert result["states_erased"] == 1  # the drained-then-forgotten record
    assert ("assistant_memory", "agent", "relay", "thread", _THREAD) not in states_pg.records
    # The pending save is settled, not left to resurface the forgotten memory.
    assert not any(o["status"] == "pending" for o in states_pg.outbox.values())


async def test_person_delete_drains_and_erases_a_pending_only_person_subject_state(
    wired, record_redis, checkpoint_saver, states_pg, monkeypatch
):
    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="relay",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel",
                routes=["chat"],
                channel="twilio",
                our_identity="+1",
                address="+1000",
                linked_at=datetime.now(UTC),
            )
        ],
    )

    record_redis.seed_person(person)
    # The states admission of a ``person`` subject resolves the identity through the conversations
    # cache manager; point it at the wired manager so the enqueue admits p1 from its seeded row.
    from tai42_skeleton.conversations import cache as cache_module

    monkeypatch.setattr(cache_module, "get_conversations_manager", ops.get_conversations_manager)
    record_redis.seed_route("chat")
    states_pg.seed_declaration(_STATE, subject_kinds=["thread", "person"])
    # The person's own cross-thread memory is a pending save with nothing landed yet.
    await _enqueue_unapplied(monkeypatch, _pending_save("relay", "person", "p1"))
    assert ("assistant_memory", "agent", "relay", "person", "p1") not in states_pg.records
    assert any(o["status"] == "pending" for o in states_pg.outbox.values())

    result = await ops.delete_conversation_person("p1")

    assert result["states_erased"] == 1
    assert ("assistant_memory", "agent", "relay", "person", "p1") not in states_pg.records
    assert not any(o["status"] == "pending" for o in states_pg.outbox.values())


async def test_person_delete_erases_person_and_aggregate_thread_state(wired, record_redis, checkpoint_saver, states_pg):
    person = Person(
        person_id="p1",
        target_kind="agent",
        target_name="relay",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel",
                routes=["chat"],
                channel="twilio",
                our_identity="+1",
                address="+1000",
                linked_at=datetime.now(UTC),
            )
        ],
    )

    record_redis.seed_person(person)
    record_redis.seed_route("chat")
    await _seed_record(message_id="pa", route_name="chat", thread_id=_PERSON_THREAD)
    states_pg.seed_declaration(_STATE, subject_kinds=["thread", "person"])
    states_pg.seed_record(_STATE, "agent", "relay", "thread", _PERSON_THREAD, {"turns": 2})
    states_pg.seed_record(_STATE, "agent", "relay", "person", "p1", {"prefs": 1})
    # A different person's state must survive.
    states_pg.seed_record(_STATE, "agent", "relay", "person", "p2", {"prefs": 2})

    result = await ops.delete_conversation_person("p1")

    assert result["states_erased"] == 2  # the person-subject AND the aggregated thread-subject
    assert ("assistant_memory", "agent", "relay", "thread", _PERSON_THREAD) not in states_pg.records
    assert ("assistant_memory", "agent", "relay", "person", "p1") not in states_pg.records
    assert ("assistant_memory", "agent", "relay", "person", "p2") in states_pg.records
