"""The event turn's store surface — the event dedupe family (its own key namespace running
the SAME claim as the channel door), the thread-membership check the event door takes before
entering a thread, the newest-record reader delivery reads a thread's identity from, and the
round-trip of an event record's ``inbound_kind`` / ``inbound_event`` / ``submitted_by``
fields against the faked redis hash + string + Lua seam."""

from __future__ import annotations

import time

import pytest

from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx


@pytest.fixture(autouse=True)
def _redis_backend(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")


def _store(monkeypatch, fake: FakeRecordRedis) -> ConversationRecordStore:
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(fake))
    return ConversationRecordStore(ConversationsSettings())


def _event_record(message_id: str = "e1", *, thread_id: str = "bridge:line:t1", **over) -> ConversationRecord:
    """A delivered event record — the shape an event turn persists once it has answered."""
    now = time.time()
    fields = {
        "message_id": message_id,
        "route_name": "line",
        "door": "channel",
        "thread_id": thread_id,
        "client_address": "+15550002222",
        "channel": "twilio",
        "our_identity": "+15550001111",
        "origin": "client",
        "inbound_kind": "event",
        "inbound_text": "",
        "inbound_event": {"event_id": "E1", "kind": "provider.update", "payload": {"n": 1}},
        "submitted_by": "svc-1",
        "answer_status": "answered",
        "answer": "handled",
        "delivery_status": DeliveryStatus.PENDING_DELIVERY,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(over)
    return ConversationRecord(**fields)  # type: ignore[arg-type]


# -- the event dedupe family --------------------------------------------------


async def test_claim_event_is_idempotent(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    first = await store.claim_event("line", "E1", "e-a")
    assert first == "e-a"  # fresh claim keeps the caller's id
    # A redelivered event of the same (route, event_id) returns the FIRST id — no second turn.
    again = await store.claim_event("line", "E1", "e-b")
    assert again == "e-a"
    # A different event id on the same route is independent.
    other = await store.claim_event("line", "E2", "e-c")
    assert other == "e-c"


async def test_get_event_owner_reads_without_claiming(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    assert await store.get_event_owner("line", "E1") is None
    assert await store.claim_event("line", "E1", "e-a") == "e-a"
    assert await store.get_event_owner("line", "E1") == "e-a"


async def test_event_family_never_collides_with_a_channel_dedupe(monkeypatch):
    store = _store(monkeypatch, FakeRecordRedis())
    # The same id string reached through both doors keys two distinct markers.
    assert await store.claim_inbound("event", "dup", "chan-owner") == "chan-owner"
    assert await store.claim_event("event", "dup", "event-owner") == "event-owner"
    assert await store.get_inbound_owner("event", "dup") == "chan-owner"
    assert await store.get_event_owner("event", "dup") == "event-owner"


# -- thread membership --------------------------------------------------------


async def test_thread_exists_is_true_only_for_a_created_thread_of_the_route(monkeypatch):
    fake = FakeRecordRedis()
    fake.seed_route("line")
    store = _store(monkeypatch, fake)
    await store.create_record(_event_record("e1", thread_id="bridge:line:t1"))
    assert await store.thread_exists("line", "bridge:line:t1") is True
    # A thread the route never created, and the same id on another route, are both absent.
    assert await store.thread_exists("line", "bridge:line:unknown") is False
    assert await store.thread_exists("other", "bridge:line:t1") is False


# -- the newest-record reader -------------------------------------------------


async def test_latest_thread_record_returns_the_newest_record(monkeypatch):
    fake = FakeRecordRedis()
    fake.seed_route("line")
    store = _store(monkeypatch, fake)
    base = time.time()
    await store.create_record(_event_record("older", thread_id="bridge:line:t1", created_at=base))
    await store.create_record(_event_record("newer", thread_id="bridge:line:t1", created_at=base + 5))
    latest = await store.latest_thread_record("line", "bridge:line:t1")
    assert latest is not None
    assert latest.message_id == "newer"
    # A thread with no record reads as ``None``, never a silent empty record.
    assert await store.latest_thread_record("line", "bridge:line:absent") is None


# -- record round-trip of the event fields ------------------------------------


async def test_event_record_round_trips_the_new_fields(monkeypatch):
    fake = FakeRecordRedis()
    fake.seed_route("line")
    store = _store(monkeypatch, fake)
    await store.create_record(_event_record("e1"))
    loaded = await store.get_record("e1")
    assert loaded is not None
    assert loaded.inbound_kind == "event"
    assert loaded.inbound_event == {"event_id": "E1", "kind": "provider.update", "payload": {"n": 1}}
    assert loaded.submitted_by == "svc-1"
    assert loaded.inbound_text == ""


async def test_message_record_carries_the_field_defaults(monkeypatch):
    fake = FakeRecordRedis()
    fake.seed_route("line")
    store = _store(monkeypatch, fake)
    now = time.time()
    record = ConversationRecord(
        message_id="m1",
        route_name="line",
        door="channel",
        thread_id="bridge:line:t1",
        client_address="+15550002222",
        channel="twilio",
        our_identity="+15550001111",
        origin="client",
        inbound_text="ask",
        answer_status="answered",
        answer="hi",
        created_at=now,
        updated_at=now,
    )
    assert record.inbound_kind == "message"
    assert record.inbound_event is None
    assert record.submitted_by is None
    await store.create_record(record)
    loaded = await store.get_record("m1")
    assert loaded is not None
    assert loaded.inbound_kind == "message"
    assert loaded.inbound_event is None
    assert loaded.submitted_by is None
