"""The filter/search OPERATIONS on top of the store scans: the thread listing's
``status``/``address`` filters and its ``truncated`` envelope field, the transcript ``q``
filter, and the admin-only route message-search leg.

Reuses the read-door test rig (the dict manager, the fake record redis, the caller stub)."""

from __future__ import annotations

import time

import pytest

from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.errors import BadRequestError, ForbiddenError, NotFoundError

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx
from .test_read_doors import _ROUTE, _as_api_route, _as_caller, _Caller, _DictManager

_THREAD = "bridge:chat:alice/user-7"


@pytest.fixture
def store(monkeypatch) -> ConversationRecordStore:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    fake = FakeRecordRedis()
    fake.seed_route(_ROUTE.route_name)
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(ops, "get_conversations_manager", lambda: _DictManager())
    return ConversationRecordStore(ConversationsSettings())


def _record(
    message_id: str, *, thread_id: str, status: DeliveryStatus, created_at: float, **over
) -> ConversationRecord:
    fields = {
        "message_id": message_id,
        "route_name": "chat",
        "door": "api",
        "thread_id": thread_id,
        "client_address": "alice/user-7",
        "callback_url": "https://cb.example/x",
        "caller_principal": "alice",
        "origin": "client",
        "inbound_text": f"ask {message_id}",
        "answer_status": "answered",
        "answer": f"answer {message_id}",
        "delivery_status": status,
        "created_at": created_at,
        "updated_at": created_at,
    }
    fields.update(over)
    return ConversationRecord(**fields)  # type: ignore[arg-type]


# -- thread listing filters --------------------------------------------------


async def test_thread_listing_filters_by_status(store, monkeypatch):
    now = time.time()
    await store.create_record(
        _record("d0", thread_id="bridge:chat:+1000", status=DeliveryStatus.DELIVERED, created_at=now)
    )
    await store.create_record(
        _record("f0", thread_id="bridge:chat:+2000", status=DeliveryStatus.FAILED, created_at=now + 1)
    )
    _as_caller(monkeypatch, _Caller("root", is_admin=True))

    listed = await ops.list_conversation_threads("chat", status="failed")

    assert [item["thread_id"] for item in listed["items"]] == ["bridge:chat:+2000"]
    assert listed["truncated"] is False


async def test_thread_listing_rejects_an_unknown_status(store, monkeypatch):
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    with pytest.raises(BadRequestError, match="status must be one of"):
        await ops.list_conversation_threads("chat", status="nope")


async def test_thread_listing_filters_by_address(store, monkeypatch):
    now = time.time()
    await store.create_record(
        _record("a0", thread_id="bridge:chat:+15550002222", status=DeliveryStatus.DELIVERED, created_at=now)
    )
    await store.create_record(
        _record("a1", thread_id="bridge:chat:+15559990000", status=DeliveryStatus.DELIVERED, created_at=now + 1)
    )
    _as_caller(monkeypatch, _Caller("root", is_admin=True))

    listed = await ops.list_conversation_threads("chat", address="2222")

    assert [item["thread_id"] for item in listed["items"]] == ["bridge:chat:+15550002222"]


async def test_the_thread_envelope_carries_the_truncated_field(store, monkeypatch):
    monkeypatch.setattr(records_module, "_FILTER_THREAD_SCAN", 1)
    now = time.time()
    for index in range(3):
        await store.create_record(
            _record(
                f"m{index}",
                thread_id=f"bridge:chat:+100{index}",
                status=DeliveryStatus.DELIVERED,
                created_at=now + index,
            )
        )
    _as_caller(monkeypatch, _Caller("root", is_admin=True))

    listed = await ops.list_conversation_threads("chat", status="failed")

    assert listed["truncated"] is True


async def test_an_unfiltered_thread_listing_is_never_truncated(store, monkeypatch):
    now = time.time()
    await store.create_record(
        _record("d0", thread_id="bridge:chat:+1000", status=DeliveryStatus.DELIVERED, created_at=now)
    )
    _as_caller(monkeypatch, _Caller("root", is_admin=True))

    listed = await ops.list_conversation_threads("chat")

    assert listed["truncated"] is False
    # An unfiltered total is still the indexed count.
    assert listed["total"] == 1


# -- transcript q filter -----------------------------------------------------


async def _seed_thread(store) -> None:
    now = time.time()
    await store.create_record(
        _record("t0", thread_id=_THREAD, status=DeliveryStatus.DELIVERED, created_at=now, inbound_text="a widget")
    )
    await store.create_record(
        _record("t1", thread_id=_THREAD, status=DeliveryStatus.DELIVERED, created_at=now + 1, inbound_text="hello")
    )


async def test_the_transcript_q_filters_and_carries_truncated(store, monkeypatch):
    _as_api_route(monkeypatch)
    await _seed_thread(store)
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))

    read = await ops.get_conversation_thread("chat", _THREAD, q="widget")

    assert [item["message_id"] for item in read["items"]] == ["t0"]
    assert read["total"] == 1
    assert read["truncated"] is False


async def test_a_transcript_q_matching_nothing_is_an_empty_page_not_a_404(store, monkeypatch):
    _as_api_route(monkeypatch)
    await _seed_thread(store)
    _as_caller(monkeypatch, _Caller("root", is_admin=True))

    read = await ops.get_conversation_thread("chat", _THREAD, q="zzz-no-match")

    assert read["items"] == []
    assert read["total"] == 0


async def test_an_unfiltered_transcript_still_404s_an_unknown_thread(store, monkeypatch):
    _as_api_route(monkeypatch)
    await _seed_thread(store)
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    with pytest.raises(NotFoundError, match="conversation thread not found"):
        await ops.get_conversation_thread("chat", "bridge:chat:nobody")


# -- route message search ----------------------------------------------------


async def test_message_search_is_admin_only(store, monkeypatch):
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))
    with pytest.raises(ForbiddenError, match="restricted to administrators"):
        await ops.search_conversation_messages("chat", q="widget")


async def test_message_search_returns_whole_records_across_threads(store, monkeypatch):
    now = time.time()
    await store.create_record(
        _record(
            "a0",
            thread_id="bridge:chat:+1000",
            status=DeliveryStatus.DELIVERED,
            created_at=now,
            inbound_text="widget a",
        )
    )
    await store.create_record(
        _record(
            "b0",
            thread_id="bridge:chat:+2000",
            status=DeliveryStatus.FAILED,
            created_at=now + 1,
            answer="widget b",
            error="boom",
        )
    )
    _as_caller(monkeypatch, _Caller("root", is_admin=True))

    found = await ops.search_conversation_messages("chat", q="widget")

    assert {item["message_id"] for item in found["items"]} == {"a0", "b0"}
    assert found["total"] == 2
    assert found["truncated"] is False
    # The whole record (admin view) — the internal ``error`` is present, as the transcript's
    # admin items carry it.
    boom = next(item for item in found["items"] if item["message_id"] == "b0")
    assert boom["error"] == "boom"


async def test_message_search_rejects_a_blank_q(store, monkeypatch):
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    with pytest.raises(BadRequestError, match="q must be a non-blank"):
        await ops.search_conversation_messages("chat", q="   ")


async def test_message_search_unknown_route_is_a_404(store, monkeypatch):
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    with pytest.raises(NotFoundError, match="conversation route not found"):
        await ops.search_conversation_messages("other-route", q="widget")
