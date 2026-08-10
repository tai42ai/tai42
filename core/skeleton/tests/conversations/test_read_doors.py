"""The caller-scoped operator read doors: one answer record by id, a thread's transcript,
the route's thread listing, and the admin-tier failed-delivery listing.

An api-door record is readable only by the caller that invoked the turn or an admin; a
channel-door record has a null ``caller_principal``, so it is admin-only. Each deny
asserts the SPECIFIC typed error.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest
from tai42_contract.access_control.models import AccessPolicy
from tai42_contract.conversations import ConversationRoute

from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.managers.base_conversations_manager import BaseConversationsManager
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations._authority import Caller
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


#: The same route name as an ``api`` row. A transcript's reader is authorized from the
#: thread's identity, and only an api-door route's threads name a principal that can own one.
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


def _authority_caller(caller_id: str) -> Caller:
    """A real :class:`Caller` for the ownership helper, which takes the resolved type."""
    return Caller(caller_id=caller_id, policy=AccessPolicy(scopes=[]), is_admin=False, owner_claim=None)


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
        answer_status="answered",
        answer="the answer",
        delivery_status=status,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def store(monkeypatch) -> ConversationRecordStore:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:6379/0")
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
    """Make ``chat`` an ``api`` row, so its threads are ones a caller can own."""
    monkeypatch.setattr(ops, "get_conversations_manager", lambda: _DictManager(_API_ROUTE))


# -- get_conversation_message: api records are caller-or-admin ----------------


async def test_api_record_readable_by_its_invoking_caller(store, monkeypatch):
    await store.create_record(_record("m1", door="api", caller_principal="alice", status=DeliveryStatus.DELIVERED))
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))
    view = await ops.get_conversation_message("chat", "m1")
    assert view["message_id"] == "m1"
    assert view["caller_principal"] == "alice"


async def test_api_record_hidden_from_another_caller(store, monkeypatch):
    await store.create_record(_record("m1", door="api", caller_principal="alice", status=DeliveryStatus.DELIVERED))
    _as_caller(monkeypatch, _Caller("bob", is_admin=False))
    with pytest.raises(ForbiddenError, match="only read conversation records from turns you invoked"):
        await ops.get_conversation_message("chat", "m1")


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


# -- get_conversation_message: channel records are admin-only -----------------


async def test_channel_record_is_admin_only(store, monkeypatch):
    await store.create_record(_record("c1", door="channel", caller_principal=None, status=DeliveryStatus.DELIVERED))
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))
    with pytest.raises(ForbiddenError, match="only read conversation records from turns you invoked"):
        await ops.get_conversation_message("chat", "c1")


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
    # A record that exists under a DIFFERENT route is a 404 (not a 403) — it is not this
    # route's record to reveal even the existence of.
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


async def test_a_thread_reads_oldest_first_for_its_owning_caller(store, monkeypatch):
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


async def test_a_thread_is_refused_to_a_caller_that_did_not_invoke_it(store, monkeypatch):
    _as_api_route(monkeypatch)
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("bob", is_admin=False))
    # The SAME answer an absent thread gets: a 403 here would tell bob that alice has a
    # thread on this route, which is the fact the door exists to protect.
    with pytest.raises(NotFoundError, match="conversation thread not found"):
        await ops.get_conversation_thread("chat", _THREAD)


async def test_a_channel_thread_is_admin_only(store, monkeypatch):
    # Nothing on a channel route names a principal a door caller can be, so its threads are
    # admin-only whatever their address looks like.
    await _seed_thread(store, caller_principal=None, door="channel")
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))
    with pytest.raises(NotFoundError, match="conversation thread not found"):
        await ops.get_conversation_thread("chat", _THREAD)


async def test_an_empty_page_of_a_foreign_thread_discloses_nothing(store, monkeypatch):
    # Authorization is decided from the thread id alone, never from the page's records, so
    # a page holding none of them is refused exactly as page 1 is. A page that ran no check
    # would answer 200 with the thread's exact record count.
    _as_api_route(monkeypatch)
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("bob", is_admin=False))

    for page in (1, 2, 99):
        with pytest.raises(NotFoundError, match="conversation thread not found"):
            await ops.get_conversation_thread("chat", _THREAD, page=page, page_size=1)


async def test_an_empty_page_of_a_channel_thread_discloses_nothing(store, monkeypatch):
    await _seed_thread(store, caller_principal=None, door="channel")
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))

    for page in (1, 2, 99):
        with pytest.raises(NotFoundError, match="conversation thread not found"):
            await ops.get_conversation_thread("chat", _THREAD, page=page, page_size=1)


async def test_an_owner_reading_past_the_end_gets_an_empty_page_not_a_refusal(store, monkeypatch):
    # The flip side: a page past the end of a thread the caller DOES own is valid data.
    _as_api_route(monkeypatch)
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))

    read = await ops.get_conversation_thread("chat", _THREAD, page=9, page_size=1)

    assert read["items"] == []
    assert read["total"] == 2
    assert read["next_page"] is None


async def test_a_principal_that_is_not_url_safe_still_owns_its_thread(store, monkeypatch):
    # An OIDC-minted principal is percent-encoded into the address the thread is keyed by;
    # ownership is decided on that same encoded form, so the caller reads its own thread.
    _as_api_route(monkeypatch)
    principal = "oidc:google:1234"
    thread = "bridge:chat:oidc%3Agoogle%3A1234/user-7"
    await store.create_record(
        _record("t0", door="api", caller_principal=principal, status=DeliveryStatus.DELIVERED, thread_id=thread)
    )
    _as_caller(monkeypatch, _Caller(principal, is_admin=False))

    read = await ops.get_conversation_thread("chat", thread)

    assert [item["message_id"] for item in read["items"]] == ["t0"]
    # And the encoded principal is not a prefix anyone else inherits.
    _as_caller(monkeypatch, _Caller("oidc", is_admin=False))
    with pytest.raises(NotFoundError, match="conversation thread not found"):
        await ops.get_conversation_thread("chat", thread)


@pytest.mark.parametrize(
    "principal",
    [
        "alice",
        # An OIDC-minted principal: every character the encoder escapes has to be escaped
        # the same way on BOTH sides, or its owner is 404'd out of its own transcript.
        "oidc:google:1234",
        "svc/robot",
        "a b+c%d",
    ],
)
async def test_the_thread_id_a_turn_mints_is_the_one_its_caller_owns(store, monkeypatch, principal):
    """Bind the api door's thread-id PRODUCER to its ownership CONSUMER.

    ``turn._api_client_address`` percent-encodes the caller principal into the address the
    thread is keyed by, and ``_caller_owns_thread`` re-encodes the caller's own principal to
    match it. Neither side is testable alone: widening the producer's ``safe`` set keeps
    every other test green while silently 404ing an OIDC-style principal out of its own
    transcript, behind a uniform 404 that names nothing.
    """
    from tai42_skeleton.conversations import turn as turn_module

    address = turn_module._api_client_address(principal, "user-7")
    thread_id = turn_module._thread_id(_API_ROUTE.route_name, address)

    _as_api_route(monkeypatch)
    await store.create_record(
        _record("t0", door="api", caller_principal=principal, status=DeliveryStatus.DELIVERED, thread_id=thread_id)
    )
    _as_caller(monkeypatch, _Caller(principal, is_admin=False))

    assert await ops._caller_owns_thread(_API_ROUTE, thread_id, _authority_caller(principal)) is True
    read = await ops.get_conversation_thread("chat", thread_id)
    assert [item["message_id"] for item in read["items"]] == ["t0"]

    # And nobody else keys it — not even a principal the encoded form starts with.
    for other in ("oidc", principal[:-1], f"{principal}x"):
        assert await ops._caller_owns_thread(_API_ROUTE, thread_id, _authority_caller(other)) is False


async def test_an_unknown_thread_is_404_before_any_authorization(store, monkeypatch):
    # A thread the route's index does not hold — never seen, or every record expired — is
    # a 404 whoever asks, so no reader learns whether it exists elsewhere.
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("bob", is_admin=False))
    with pytest.raises(NotFoundError, match="conversation thread not found"):
        await ops.get_conversation_thread("chat", "bridge:chat:nobody")
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    with pytest.raises(NotFoundError, match="conversation route not found"):
        await ops.get_conversation_thread("other-route", _THREAD)


async def test_an_unknown_route_tells_a_non_admin_nothing_the_uniform_404_does_not(store, monkeypatch):
    # The transcript door's whole point is that a refusal names nothing. A distinct
    # "route not found" would answer WHICH route names exist to a caller that owns no
    # thread on any of them — the same oracle read from one step further out.
    await _seed_thread(store, caller_principal="alice")
    _as_caller(monkeypatch, _Caller("bob", is_admin=False))
    with pytest.raises(NotFoundError, match="conversation thread not found"):
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
