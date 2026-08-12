"""The operator read doors: one answer record by id, a thread's transcript, the route's
thread listing, and the admin-tier failed-delivery listing.

The record and transcript reads are grant-gated: any authenticated grant-holder reads
them, an admin getting the whole record and a non-admin the caller-safe projection. The
thread and failed-delivery LISTINGS are admin-only. Each deny asserts the SPECIFIC typed
error.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest
from tai42_contract.conversations import ConversationRoute

from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.managers.base_conversations_manager import BaseConversationsManager
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.errors import BadRequestError, ForbiddenError, NotFoundError

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

#: The one route the fake routing table holds, so a read on any other name is a 404.
_ROUTE = ConversationRoute(
    route_name="chat",
    door="channel",
    target_kind="agent",
    target_name="relay",
    execution_key="svc",
    channel="twilio",
    our_identity="+15550001111",
    execution_key_fingerprint="fp",
)


#: The same route name as an ``api`` row, for reads exercised over an api-door thread.
_API_ROUTE = ConversationRoute(
    route_name="chat",
    door="api",
    target_kind="agent",
    target_name="relay",
    execution_key="svc",
    callback_url="https://cb.example/x",
    execution_key_fingerprint="fp",
)


class _DictManager(BaseConversationsManager):
    """A non-in-memory routing-row store so ``_require_backend`` admits the read door."""

    def __init__(self, route: ConversationRoute = _ROUTE) -> None:
        super().__init__(ConversationsSettings())
        self._route = route

    async def put_route(self, route):
        raise NotImplementedError

    async def get_route(self, route_name):
        return self._route if route_name == self._route.route_name else None

    async def delete_route(self, route_name):
        return False

    async def list_routes(self):
        return {self._route.route_name: self._route}


@dataclass
class _Caller:
    caller_id: str
    is_admin: bool


def _record(
    message_id: str,
    *,
    door: str,
    caller_principal: str | None,
    status: DeliveryStatus,
    error: str | None = None,
    thread_id: str | None = None,
    created_at: float | None = None,
) -> ConversationRecord:
    now = created_at if created_at is not None else time.time()
    return ConversationRecord(
        error=error,
        message_id=message_id,
        route_name="chat",
        door=door,  # type: ignore[arg-type]
        thread_id=thread_id or f"bridge:chat:{message_id}",
        client_address="user-7",
        inbound_text=f"ask {message_id}",
        channel="twilio" if door == "channel" else None,
        our_identity="+15550001111" if door == "channel" else None,
        callback_url="https://cb.example/x" if door == "api" else None,
        caller_principal=caller_principal,
        origin="client",
        answer_status="answered",
        answer="the answer",
        delivery_status=status,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def store(monkeypatch) -> ConversationRecordStore:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    fake = FakeRecordRedis()
    # The create writes the thread indexes only while the route still routes, and the
    # fake routing table these doors read holds exactly this one name.
    fake.seed_route(_ROUTE.route_name)
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(fake))
    monkeypatch.setattr(ops, "get_conversations_manager", lambda: _DictManager())
    return ConversationRecordStore(ConversationsSettings())


def _as_caller(monkeypatch, caller: _Caller) -> None:
    async def _resolve():
        return caller

    monkeypatch.setattr(ops, "resolve_caller", _resolve)


def _as_api_route(monkeypatch) -> None:
    """Make ``chat`` an ``api`` row, for a read over an api-door thread."""
    monkeypatch.setattr(ops, "get_conversations_manager", lambda: _DictManager(_API_ROUTE))


# -- get_conversation_message: a record reads for any grant-holder ------------


async def test_api_record_readable_by_its_invoking_caller(store, monkeypatch):
    await store.create_record(_record("m1", door="api", caller_principal="alice", status=DeliveryStatus.DELIVERED))
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))
    view = await ops.get_conversation_message("chat", "m1")
    assert view["message_id"] == "m1"
    assert view["caller_principal"] == "alice"


async def test_api_record_readable_by_another_grant_holder_as_projection(store, monkeypatch):
    # Grant-gated: another authenticated non-admin grant-holder reads the record too, as the
    # caller-safe projection that withholds the route key's run detail.
    await store.create_record(_record("m1", door="api", caller_principal="alice", status=DeliveryStatus.DELIVERED))
    _as_caller(monkeypatch, _Caller("bob", is_admin=False))
    view = await ops.get_conversation_message("chat", "m1")
    assert view["message_id"] == "m1"
    assert "error" not in view


async def test_api_record_readable_by_admin(store, monkeypatch):
    await store.create_record(_record("m1", door="api", caller_principal="alice", status=DeliveryStatus.DELIVERED))
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    view = await ops.get_conversation_message("chat", "m1")
    assert view["message_id"] == "m1"


# -- the caller reads a projection, the admin reads the record ----------------

#: The internal detail of a turn that ran as the ROUTE's execution key — a principal the
#: invoking caller has no authority over — plus the delivery machine's own bookkeeping.
_INTERNAL_FIELDS = ("error", "attempts", "outbound_message_ids", "callback_url", "channel", "our_identity")


async def test_a_non_admin_caller_never_reads_the_internal_turn_detail(store, monkeypatch):
    # A denied turn stores the raw refusal, which names the route key's grants; the
    # caller reads the outcome and nothing about that principal.
    denial = "turn denied: access denied: POST /api/agents/foo/runs is not permitted for 'metrics-svc'"
    await store.create_record(
        _record("m1", door="api", caller_principal="alice", status=DeliveryStatus.FAILED, error=denial)
    )
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))
    view = await ops.get_conversation_message("chat", "m1")

    assert view["message_id"] == "m1"
    assert view["answer"] == "the answer"
    assert view["delivery_status"] == "failed"
    for field in _INTERNAL_FIELDS:
        assert field not in view
    assert "metrics-svc" not in repr(view)


async def test_an_admin_reads_the_whole_record_including_the_internal_detail(store, monkeypatch):
    denial = "turn denied: access denied for 'metrics-svc'"
    await store.create_record(
        _record("m1", door="api", caller_principal="alice", status=DeliveryStatus.FAILED, error=denial)
    )
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    view = await ops.get_conversation_message("chat", "m1")

    assert view["error"] == denial
    for field in _INTERNAL_FIELDS:
        assert field in view


# -- get_conversation_message: a channel record reads as the projection -------


async def test_channel_record_readable_by_a_grant_holder_as_projection(store, monkeypatch):
    # A channel record names no caller principal; grant-gated, a non-admin grant-holder still
    # reads it as the caller-safe projection.
    await store.create_record(_record("c1", door="channel", caller_principal=None, status=DeliveryStatus.DELIVERED))
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))
    view = await ops.get_conversation_message("chat", "c1")
    assert view["message_id"] == "c1"
    assert "error" not in view


async def test_channel_record_readable_by_admin(store, monkeypatch):
    await store.create_record(_record("c1", door="channel", caller_principal=None, status=DeliveryStatus.DELIVERED))
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    view = await ops.get_conversation_message("chat", "c1")
    assert view["message_id"] == "c1"


async def test_unknown_or_cross_route_record_is_404(store, monkeypatch):
    await store.create_record(_record("m1", door="api", caller_principal="alice", status=DeliveryStatus.DELIVERED))
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))
    with pytest.raises(NotFoundError, match="conversation record not found"):
        await ops.get_conversation_message("chat", "missing")
    # A record that exists under a DIFFERENT route is a 404 — it is not this route's record
    # to reveal even the existence of.
    with pytest.raises(NotFoundError, match="conversation record not found"):
        await ops.get_conversation_message("other-route", "m1")


# -- the projections publish the inbound text ---------------------------------


async def test_both_projections_carry_the_inbound_text(store, monkeypatch):
    await store.create_record(_record("m1", door="api", caller_principal="alice", status=DeliveryStatus.DELIVERED))
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))
    assert (await ops.get_conversation_message("chat", "m1"))["inbound_text"] == "ask m1"
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    assert (await ops.get_conversation_message("chat", "m1"))["inbound_text"] == "ask m1"


# -- list_conversation_threads: admin-tier ------------------------------------


async def test_thread_listing_is_admin_only(store, monkeypatch):
    await store.create_record(_record("m1", door="api", caller_principal="alice", status=DeliveryStatus.DELIVERED))
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))
    with pytest.raises(ForbiddenError, match="restricted to administrators"):
        await ops.list_conversation_threads("chat")


async def test_thread_listing_summarizes_and_pages(store, monkeypatch):
    now = time.time()
    for index in range(3):
        await store.create_record(
            _record(
                f"m{index}",
                door="channel",
                caller_principal=None,
                status=DeliveryStatus.DELIVERED,
                thread_id=f"bridge:chat:+1555000{index}",
                created_at=now + index,
            )
        )
    _as_caller(monkeypatch, _Caller("root", is_admin=True))

    first = await ops.list_conversation_threads("chat", page=1, page_size=2)
    assert first["total"] == 3
    assert first["next_page"] == 2
    assert [item["thread_id"] for item in first["items"]] == ["bridge:chat:+15550002", "bridge:chat:+15550001"]
    assert first["items"][0]["message_count"] == 1
    assert first["items"][0]["last_delivery_status"] == "delivered"
    assert first["items"][0]["client_address"] == "user-7"

    last = await ops.list_conversation_threads("chat", page=2, page_size=2)
    assert [item["thread_id"] for item in last["items"]] == ["bridge:chat:+15550000"]
    assert last["next_page"] is None


async def test_thread_listing_refuses_an_unknown_route_and_a_bad_page(store, monkeypatch):
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    with pytest.raises(NotFoundError, match="conversation route not found"):
        await ops.list_conversation_threads("other-route")
    with pytest.raises(BadRequestError, match="must be >= 1"):
        await ops.list_conversation_threads("chat", page=0)
    with pytest.raises(BadRequestError, match="must be >= 1"):
        await ops.list_conversation_threads("chat", page=1, page_size=0)


async def test_a_page_size_above_the_cap_is_capped_not_refused(store, monkeypatch):
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    listed = await ops.list_conversation_threads("chat", page=1, page_size=10_000)
    assert listed["page_size"] == ops.MAX_THREAD_PAGE_SIZE


# -- get_conversation_thread: the transcript ----------------------------------

_THREAD = "bridge:chat:alice/user-7"


async def _seed_thread(store, *, caller_principal: str | None, door: str = "api") -> None:
    now = time.time()
    for index in range(2):
        await store.create_record(
            _record(
                f"t{index}",
                door=door,
                caller_principal=caller_principal,
                status=DeliveryStatus.DELIVERED,
                thread_id=_THREAD,
                created_at=now + index,
            )
        )


async def test_a_thread_reads_oldest_first_for_a_grant_holder(store, monkeypatch):
    _as_api_route(monkeypatch)
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))

    read = await ops.get_conversation_thread("chat", _THREAD)

    assert [item["message_id"] for item in read["items"]] == ["t0", "t1"]
    assert read["total"] == 2
    assert read["next_page"] is None
    assert read["order"] == "asc"
    assert read["items"][0]["inbound_text"] == "ask t0"
    # The caller-safe projection, not the admin one.
    assert "error" not in read["items"][0]


async def test_an_admin_reads_the_whole_record_of_every_thread(store, monkeypatch):
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("root", is_admin=True))

    read = await ops.get_conversation_thread("chat", _THREAD)

    assert "error" in read["items"][0]
    assert "attempts" in read["items"][0]


async def test_a_thread_reads_for_another_grant_holder(store, monkeypatch):
    # Grant-gated: a non-admin who did not invoke the turns still reads the thread, as the
    # caller-safe projection.
    _as_api_route(monkeypatch)
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("bob", is_admin=False))

    read = await ops.get_conversation_thread("chat", _THREAD)

    assert [item["message_id"] for item in read["items"]] == ["t0", "t1"]
    assert "error" not in read["items"][0]


async def test_a_channel_thread_reads_for_a_grant_holder(store, monkeypatch):
    # A channel thread names no caller principal; grant-gated, a non-admin grant-holder still
    # reads it as the caller-safe projection.
    await _seed_thread(store, caller_principal=None, door="channel")
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))

    read = await ops.get_conversation_thread("chat", _THREAD)

    assert [item["message_id"] for item in read["items"]] == ["t0", "t1"]
    assert "error" not in read["items"][0]


async def test_reading_past_the_end_gets_an_empty_page_not_a_refusal(store, monkeypatch):
    # A page past the end of a thread that reads is valid data, never a 404.
    _as_api_route(monkeypatch)
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))

    read = await ops.get_conversation_thread("chat", _THREAD, page=9, page_size=1)

    assert read["items"] == []
    assert read["total"] == 2
    assert read["next_page"] is None


async def test_an_unknown_thread_is_404(store, monkeypatch):
    # A thread the route's index does not hold — never seen, or every record expired — is a
    # 404 whoever asks. An unknown route is its own loud 404.
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("bob", is_admin=False))
    with pytest.raises(NotFoundError, match="conversation thread not found"):
        await ops.get_conversation_thread("chat", "bridge:chat:nobody")
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    with pytest.raises(NotFoundError, match="conversation route not found"):
        await ops.get_conversation_thread("other-route", _THREAD)


async def test_an_unknown_route_is_a_loud_404_for_a_non_admin_too(store, monkeypatch):
    # Grant-gated reads disclose route existence: a non-admin asking an unknown route gets the
    # loud route not-found, the same as an admin.
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("bob", is_admin=False))
    with pytest.raises(NotFoundError, match="conversation route not found"):
        await ops.get_conversation_thread("other-route", _THREAD)


async def test_the_thread_listing_authorizes_before_it_looks_the_route_up(store, monkeypatch):
    # 404 for an unknown route and 403 for a known one is a name oracle decided BEFORE
    # authorization: a non-admin is refused identically either way.
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))
    with pytest.raises(ForbiddenError, match="restricted to administrators"):
        await ops.list_conversation_threads("other-route")
    with pytest.raises(ForbiddenError, match="restricted to administrators"):
        await ops.list_conversation_threads("chat")


async def test_the_transcript_pages(store, monkeypatch):
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("root", is_admin=True))

    first = await ops.get_conversation_thread("chat", _THREAD, page=1, page_size=1)
    second = await ops.get_conversation_thread("chat", _THREAD, page=2, page_size=1)

    assert [item["message_id"] for item in first["items"]] == ["t0"]
    assert first["next_page"] == 2
    assert [item["message_id"] for item in second["items"]] == ["t1"]
    assert second["next_page"] is None


async def test_the_transcript_tails_newest_first_on_request(store, monkeypatch):
    # The live-tail order a 5s monitor poll needs: page 1 is always the latest messages,
    # so a thread past one page still shows what just arrived.
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("root", is_admin=True))

    first = await ops.get_conversation_thread("chat", _THREAD, page=1, page_size=1, order="desc")
    second = await ops.get_conversation_thread("chat", _THREAD, page=2, page_size=1, order="desc")

    assert [item["message_id"] for item in first["items"]] == ["t1"]
    assert first["order"] == "desc"
    assert first["next_page"] == 2
    assert [item["message_id"] for item in second["items"]] == ["t0"]
    assert second["next_page"] is None


async def test_the_transcript_refuses_an_unknown_order(store, monkeypatch):
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    with pytest.raises(BadRequestError, match="order must be one of"):
        await ops.get_conversation_thread("chat", _THREAD, order="newest")


async def test_the_transcript_refuses_a_blank_thread_id(store, monkeypatch):
    # A blank id builds a well-formed key every other blank builds; the store raises a bare
    # ValueError on it, which would surface as an unmapped 500.
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    for blank in ("", "   "):
        with pytest.raises(BadRequestError, match="thread_id must be a non-blank"):
            await ops.get_conversation_thread("chat", blank)


async def test_the_transcript_refuses_a_page_past_the_served_maximum(store, monkeypatch):
    # A page that big names an offset the index cannot be sliced at, which the backend
    # answers with an error of its own — a malformed window, refused as one.
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    with pytest.raises(BadRequestError, match="page must be <="):
        await ops.get_conversation_thread("chat", _THREAD, page=2**62)
    with pytest.raises(BadRequestError, match="page must be <="):
        await ops.list_conversation_threads("chat", page=2**62)


async def test_the_transcript_refuses_a_bad_page(store, monkeypatch):
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    with pytest.raises(BadRequestError, match="must be >= 1"):
        await ops.get_conversation_thread("chat", _THREAD, page=0)


# -- list_failed_conversations: admin-tier ------------------------------------


async def test_failed_listing_is_admin_only(store, monkeypatch):
    await store.create_record(_record("f1", door="api", caller_principal="alice", status=DeliveryStatus.FAILED))
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))
    with pytest.raises(ForbiddenError, match="restricted to administrators"):
        await ops.list_failed_conversations()


async def test_failed_listing_returns_only_failed_records_for_admin(store, monkeypatch):
    await store.create_record(_record("f1", door="api", caller_principal="alice", status=DeliveryStatus.FAILED))
    await store.create_record(_record("d1", door="api", caller_principal="alice", status=DeliveryStatus.DELIVERED))
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    listed = await ops.list_failed_conversations()
    assert listed["total"] == 1
    assert listed["items"][0]["message_id"] == "f1"
