"""Erasing a linked person ENTIRELY — the ``delete_conversation_person`` op and the store
``erase`` beneath it.

The proof is that every person-scoped store is gone after the door runs: the aggregated
``bridge:@person:{id}`` thread (checkpoint, records, indexes), the person row, and every
address→person index mapping. The door is idempotent (a second run is not a 404), refuses a
turn in flight (409), and is a loud 501 when the checkpoint provider cannot forget a thread.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from tai42_contract.conversations import Person, PersonAddress

from tai42_skeleton.conversations import persons as persons_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.persons import ConversationPersonStore, _address_key_of
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.errors import BadRequestError, ConflictError, NotSupportedError

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx
from .test_read_doors import _DictManager

_PERSON_ID = "p1"
_PERSON_THREAD = "bridge:@person:p1"


def _person() -> Person:
    return Person(
        person_id=_PERSON_ID,
        target_kind="agent",
        target_name="relay",
        created_at=datetime.now(UTC),
        addresses=[
            PersonAddress(
                door="channel", routes=["chat-a"], channel="twilio", our_identity="+1", address="+1000",
                linked_at=datetime.now(UTC),
            ),
            PersonAddress(
                door="channel", routes=["chat-b"], channel="whatsapp", our_identity="+2", address="+2000",
                linked_at=datetime.now(UTC),
            ),
        ],
    )


@pytest.fixture
def fake(monkeypatch) -> FakeRecordRedis:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    fake = FakeRecordRedis()
    ctx = make_record_client_ctx(fake)
    monkeypatch.setattr(records_module, "client_ctx", ctx)
    monkeypatch.setattr(persons_module, "client_ctx", ctx)
    monkeypatch.setattr(ops, "get_conversations_manager", lambda: _DictManager())
    return fake


class _RecordingSaver:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


class _FakeRegistry:
    def __init__(self, saver) -> None:
        self._saver = saver

    async def get_checkpointer(self, provider, conn_string):
        return self._saver


class _FakeProviderSettings:
    checkpoint = "memory"
    checkpoint_conn_string = None


@pytest.fixture
def checkpoint_saver(monkeypatch) -> _RecordingSaver:
    import tai42_kit.llm.checkpoint.checkpoint_registry as registry_mod
    import tai42_kit.llm.settings as settings_mod

    saver = _RecordingSaver()
    monkeypatch.setattr(registry_mod, "checkpoint_registry", lambda: _FakeRegistry(saver))
    monkeypatch.setattr(settings_mod, "llm_provider_settings", lambda: _FakeProviderSettings())
    return saver


def _seed_person(fake: FakeRecordRedis, person: Person) -> None:
    settings = ConversationsSettings()
    fake._strings[settings.person_key(person.person_id)] = person.model_dump_json()
    for address in person.addresses:
        index_key = settings.person_index_key(person.target_kind, person.target_name)
        fake._hashes.setdefault(index_key, {})[_address_key_of(address)] = person.person_id


async def _seed_record(route_name: str, message_id: str) -> None:
    now = time.time()
    await ConversationRecordStore(ConversationsSettings()).create_record(
        ConversationRecord(
            message_id=message_id,
            route_name=route_name,
            door="channel",
            thread_id=_PERSON_THREAD,
            client_address="+1000",
            channel="twilio",
            our_identity="+1000",
            origin="client",
            inbound_text="ask",
            answer_status="answered",
            answer="the answer",
            delivery_status=DeliveryStatus.DELIVERED,
            created_at=now,
            updated_at=now,
        )
    )


async def _seed_accepted(route_name: str, message_id: str) -> None:
    now = time.time()
    await ConversationRecordStore(ConversationsSettings()).create_record(
        ConversationRecord(
            message_id=message_id,
            route_name=route_name,
            door="channel",
            thread_id=_PERSON_THREAD,
            client_address="+1000",
            channel="twilio",
            our_identity="+1000",
            origin="client",
            inbound_text="ask",
            delivery_status=DeliveryStatus.ACCEPTED,
            created_at=now,
            updated_at=now,
        ),
        intake_token="tok",
    )


# -- store erase -------------------------------------------------------------


async def test_store_erase_removes_the_row_and_its_index_mappings(fake):
    person = _person()
    _seed_person(fake, person)
    settings = ConversationsSettings()

    row_removed, fields_removed = await ConversationPersonStore(settings).erase(person)

    assert row_removed is True
    assert fields_removed == 2
    assert settings.person_key(_PERSON_ID) not in fake._strings
    assert fake._hashes.get(settings.person_index_key("agent", "relay"), {}) == {}


async def test_store_erase_is_idempotent(fake):
    person = _person()
    _seed_person(fake, person)
    store = ConversationPersonStore(ConversationsSettings())
    await store.erase(person)

    row_removed, fields_removed = await store.erase(person)

    assert row_removed is False
    assert fields_removed == 0


# -- the op ------------------------------------------------------------------


async def test_delete_person_erases_every_person_scoped_store(fake, checkpoint_saver):
    person = _person()
    _seed_person(fake, person)
    fake.seed_route("chat-a")
    fake.seed_route("chat-b")
    await _seed_record("chat-a", "pa")
    await _seed_record("chat-b", "pb")
    settings = ConversationsSettings()
    # A mode override on the aggregated thread, so its teardown by ``drop_thread`` is asserted.
    fake._strings[settings.mode_key(_PERSON_THREAD)] = "manual"

    result = await ops.delete_conversation_person(_PERSON_ID)

    assert result == {"person_id": _PERSON_ID, "removed": 2, "erased": True}
    # a. the aggregated agent checkpoint — forgotten once.
    assert checkpoint_saver.deleted == [_PERSON_THREAD]
    # b. the answer records + both route indexes + the thread's mode override.
    assert settings.record_key("pa") not in fake._hashes
    assert settings.record_key("pb") not in fake._hashes
    assert settings.mode_key(_PERSON_THREAD) not in fake._strings
    for route_name in ("chat-a", "chat-b"):
        assert settings.thread_index_key(route_name, _PERSON_THREAD) not in fake._zsets
        assert _PERSON_THREAD not in fake._zsets.get(settings.route_threads_key(route_name), {})
    # c. the person row.
    assert settings.person_key(_PERSON_ID) not in fake._strings
    # d. every address→person index mapping.
    assert fake._hashes.get(settings.person_index_key("agent", "relay"), {}) == {}


async def test_delete_person_is_idempotent(fake, checkpoint_saver):
    person = _person()
    _seed_person(fake, person)
    fake.seed_route("chat-a")
    fake.seed_route("chat-b")
    await _seed_record("chat-a", "pa")

    first = await ops.delete_conversation_person(_PERSON_ID)
    assert first["erased"] is True

    # The row is gone; a retry is not a 404 — the checkpoint is forgotten again and erased is
    # False, so a partial run is finished rather than surfacing an error.
    second = await ops.delete_conversation_person(_PERSON_ID)
    assert second == {"person_id": _PERSON_ID, "removed": 0, "erased": False}
    assert checkpoint_saver.deleted == [_PERSON_THREAD, _PERSON_THREAD]


async def test_delete_unknown_person_forgets_the_checkpoint_with_erased_false(fake, checkpoint_saver):
    result = await ops.delete_conversation_person("never-seen")

    assert result == {"person_id": "never-seen", "removed": 0, "erased": False}
    assert checkpoint_saver.deleted == ["bridge:@person:never-seen"]


async def test_delete_person_with_a_turn_in_flight_is_a_409(fake, checkpoint_saver):
    person = _person()
    _seed_person(fake, person)
    fake.seed_route("chat-a")
    fake.seed_route("chat-b")
    await _seed_accepted("chat-a", "live")
    settings = ConversationsSettings()

    with pytest.raises(ConflictError, match="turn in flight"):
        await ops.delete_conversation_person(_PERSON_ID)
    # Refused before any teardown: the checkpoint, the record and the person row all stand.
    assert checkpoint_saver.deleted == []
    assert settings.record_key("live") in fake._hashes
    assert settings.person_key(_PERSON_ID) in fake._strings


async def test_delete_person_blank_id_is_a_400(fake):
    with pytest.raises(BadRequestError, match="non-blank person identifier"):
        await ops.delete_conversation_person("   ")


async def test_delete_person_is_a_loud_501_without_a_checkpoint_deleter(fake, monkeypatch):
    person = _person()
    _seed_person(fake, person)
    fake.seed_route("chat-a")

    class _NoDeleteSaver:
        pass  # exposes no adelete_thread

    import tai42_kit.llm.checkpoint.checkpoint_registry as registry_mod
    import tai42_kit.llm.settings as settings_mod

    monkeypatch.setattr(registry_mod, "checkpoint_registry", lambda: _FakeRegistry(_NoDeleteSaver()))
    monkeypatch.setattr(settings_mod, "llm_provider_settings", lambda: _FakeProviderSettings())

    with pytest.raises(NotSupportedError, match="adelete_thread"):
        await ops.delete_conversation_person(_PERSON_ID)


async def test_delete_person_501_without_a_backend(monkeypatch):
    monkeypatch.delenv("CONVERSATIONS_REDIS_URL", raising=False)
    from tai42_skeleton.conversations.managers.in_memory_conversations_manager import InMemoryConversationsManager

    monkeypatch.setattr(
        ops, "get_conversations_manager", lambda: InMemoryConversationsManager(ConversationsSettings())
    )
    with pytest.raises(NotSupportedError):
        await ops.delete_conversation_person(_PERSON_ID)
