"""The LINKED person's AGGREGATED transcript door and its authorization.

A ``bridge:@person:{id}`` thread's records live in one per-route index PER ROUTE; the read is
a k-way merge across them, so one full created_at-ordered history is served (never a partial
leg), and a grant-holder reads BOTH the api-leg records and the merged channel-leg records.
The read is grant-gated: any authenticated grant-holder reads it, an admin the whole records
and a non-admin the caller-safe projection. The supplied ``route_name`` must be one of the
person's routes; an unknown route is its own loud 404, and a target mismatch or a route the
person never wrote under answers the one uniform not-found."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from tai42_contract.conversations import ConversationRoute, Person, PersonAddress

from tai42_skeleton.agent.thread_reservation import PERSON_THREAD_PREFIX
from tai42_skeleton.conversations import persons as persons_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.managers.base_conversations_manager import BaseConversationsManager
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations import conversations as ops
from tai42_skeleton.operations.errors import NotFoundError

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

_PERSON_ID = "P1"
_THREAD = f"{PERSON_THREAD_PREFIX}{_PERSON_ID}"


def _api_route(route_name: str) -> ConversationRoute:
    return ConversationRoute(
        route_name=route_name,
        door="api",
        target_kind="agent",
        target_name="assistant",
        execution_key="svc",
        callback_url="https://cb.example/x",
        execution_key_fingerprint="fp",
    )


def _channel_route(route_name: str) -> ConversationRoute:
    return ConversationRoute(
        route_name=route_name,
        door="channel",
        target_kind="agent",
        target_name="assistant",
        execution_key="svc",
        channel="twilio",
        our_identity="+15550009999",
        execution_key_fingerprint="fp",
    )


class _DictManager(BaseConversationsManager):
    def __init__(self, routes: dict[str, ConversationRoute]) -> None:
        super().__init__(ConversationsSettings())
        self._routes = routes

    async def put_route(self, route):
        raise NotImplementedError

    async def get_route(self, route_name):
        return self._routes.get(route_name)

    async def delete_route(self, route_name):
        return False

    async def list_routes(self):
        return dict(self._routes)


@dataclass
class _Caller:
    caller_id: str | None
    is_admin: bool


def _addr(*, door, address, routes, channel=None, our_identity=None) -> PersonAddress:
    return PersonAddress(
        door=door,
        routes=list(routes),
        channel=channel,
        our_identity=our_identity,
        address=address,
        linked_at=datetime.now(UTC),
    )


def _person(addresses: list[PersonAddress]) -> Person:
    return Person(
        person_id=_PERSON_ID,
        target_kind="agent",
        target_name="assistant",
        created_at=datetime.now(UTC),
        addresses=addresses,
    )


def _person_record(
    message_id: str, route_name: str, *, door, created_at: float, caller_principal=None
) -> ConversationRecord:
    return ConversationRecord(
        message_id=message_id,
        route_name=route_name,
        door=door,
        thread_id=_THREAD,
        client_address="alice/u7" if door == "api" else "+2000",
        channel=None if door == "api" else "twilio",
        our_identity=None if door == "api" else "+15550009999",
        callback_url="https://cb.example/x" if door == "api" else None,
        caller_principal=caller_principal,
        inbound_text=f"ask {message_id}",
        answer_status="answered",
        answer=f"answer {message_id}",
        delivery_status=DeliveryStatus.DELIVERED,
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.fixture
def fake(monkeypatch) -> FakeRecordRedis:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:6379/0")
    fk = FakeRecordRedis()
    ctx = make_record_client_ctx(fk)
    monkeypatch.setattr(records_module, "client_ctx", ctx)
    monkeypatch.setattr(persons_module, "client_ctx", ctx)
    return fk


def _seed_person(fake: FakeRecordRedis, person: Person) -> None:
    fake._strings[ConversationsSettings().person_key(person.person_id)] = person.model_dump_json()


def _as_caller(monkeypatch, caller: _Caller) -> None:
    async def _resolve():
        return caller

    monkeypatch.setattr(ops, "resolve_caller", _resolve)


def _wire_manager(monkeypatch, *routes: ConversationRoute) -> None:
    monkeypatch.setattr(ops, "get_conversations_manager", lambda: _DictManager({r.route_name: r for r in routes}))


def _store() -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


# -- records.list_person_thread_records: the k-way merge ---------------------


async def test_aggregated_read_merges_two_routes_in_both_orders(fake):
    fake.seed_route("line-a")
    fake.seed_route("line-b")
    store = _store()
    # Interleaved created_at across two routes.
    await store.create_record(_person_record("a1", "line-a", door="channel", created_at=10.0))
    await store.create_record(_person_record("b1", "line-b", door="channel", created_at=20.0))
    await store.create_record(_person_record("a2", "line-a", door="channel", created_at=30.0))
    await store.create_record(_person_record("b2", "line-b", door="channel", created_at=40.0))

    asc = await store.list_person_thread_records(["line-a", "line-b"], _THREAD, offset=0, limit=10)
    assert [r.message_id for r in asc.records] == ["a1", "b1", "a2", "b2"]
    assert asc.total == 4

    desc = await store.list_person_thread_records(["line-a", "line-b"], _THREAD, offset=0, limit=10, newest_first=True)
    assert [r.message_id for r in desc.records] == ["b2", "a2", "b1", "a1"]


async def test_aggregated_read_pages_across_the_index_boundary(fake):
    fake.seed_route("line-a")
    fake.seed_route("line-b")
    store = _store()
    await store.create_record(_person_record("a1", "line-a", door="channel", created_at=10.0))
    await store.create_record(_person_record("b1", "line-b", door="channel", created_at=20.0))
    await store.create_record(_person_record("a2", "line-a", door="channel", created_at=30.0))

    page1 = await store.list_person_thread_records(["line-a", "line-b"], _THREAD, offset=0, limit=2)
    page2 = await store.list_person_thread_records(["line-a", "line-b"], _THREAD, offset=2, limit=2)
    assert [r.message_id for r in page1.records] == ["a1", "b1"]
    assert [r.message_id for r in page2.records] == ["a2"]
    assert page1.total == page2.total == 3


async def test_aggregated_read_breaks_equal_created_at_ties_deterministically(fake):
    fake.seed_route("line-a")
    fake.seed_route("line-b")
    store = _store()
    # Two routes, SAME created_at: the message_id tie-break mirrors redis equal-score order.
    await store.create_record(_person_record("m-aaa", "line-a", door="channel", created_at=50.0))
    await store.create_record(_person_record("m-bbb", "line-b", door="channel", created_at=50.0))

    asc = await store.list_person_thread_records(["line-a", "line-b"], _THREAD, offset=0, limit=10)
    assert [r.message_id for r in asc.records] == ["m-aaa", "m-bbb"]
    desc = await store.list_person_thread_records(["line-a", "line-b"], _THREAD, offset=0, limit=10, newest_first=True)
    assert [r.message_id for r in desc.records] == ["m-bbb", "m-aaa"]


async def test_aggregated_read_of_an_empty_person_is_total_zero(fake):
    fake.seed_route("line-a")
    page = await _store().list_person_thread_records(["line-a"], _THREAD, offset=0, limit=10)
    assert page.total == 0
    assert page.records == []


async def test_aggregated_read_skips_a_missing_or_corrupt_row_keeping_the_total(fake):
    # A member whose record row is gone, and one whose row is unparseable, are both skipped
    # (left for the prune pass) while the indexed ``total`` still counts them.
    fake.seed_route("line-a")
    store = _store()
    await store.create_record(_person_record("a1", "line-a", door="channel", created_at=10.0))
    settings = ConversationsSettings()
    thread_key = settings.thread_index_key("line-a", _THREAD)
    fake._zsets.setdefault(thread_key, {})["missing"] = 20.0  # indexed, no row
    fake._zsets[thread_key]["corrupt"] = 30.0
    fake.seed_hash(settings.record_key("corrupt"), {"data": "{}"})  # indexed, unparseable row

    page = await store.list_person_thread_records(["line-a"], _THREAD, offset=0, limit=10)
    assert [r.message_id for r in page.records] == ["a1"]
    assert page.total == 3


# -- the transcript door: authz + both directions ----------------------------


async def test_api_caller_reads_both_its_api_leg_and_the_channel_leg(fake, monkeypatch):
    fake.seed_route("chat-api")
    fake.seed_route("line-b")
    store = _store()
    await store.create_record(
        _person_record("api-1", "chat-api", door="api", caller_principal="alice", created_at=10.0)
    )
    await store.create_record(_person_record("chan-1", "line-b", door="channel", created_at=20.0))
    _seed_person(
        fake,
        _person(
            [
                _addr(door="api", address="alice/u7", routes=["chat-api"]),
                _addr(
                    door="channel",
                    address="+2000",
                    routes=["line-b"],
                    channel="twilio",
                    our_identity="+15550009999",
                ),
            ]
        ),
    )
    _wire_manager(monkeypatch, _api_route("chat-api"), _channel_route("line-b"))
    _as_caller(monkeypatch, _Caller("alice", is_admin=False))

    read = await ops.get_conversation_thread("chat-api", _THREAD, order="asc")
    # BOTH legs, one merged history — the api leg AND the channel-leg record.
    assert [item["message_id"] for item in read["items"]] == ["api-1", "chan-1"]
    assert read["total"] == 2


async def test_another_grant_holder_reads_the_person_thread(fake, monkeypatch):
    # Grant-gated: a non-admin who is not the person still reads the aggregated thread, as the
    # caller-safe projection.
    fake.seed_route("chat-api")
    store = _store()
    await store.create_record(
        _person_record("api-1", "chat-api", door="api", caller_principal="alice", created_at=10.0)
    )
    _seed_person(fake, _person([_addr(door="api", address="alice/u7", routes=["chat-api"])]))
    _wire_manager(monkeypatch, _api_route("chat-api"))
    _as_caller(monkeypatch, _Caller("bob", is_admin=False))
    read = await ops.get_conversation_thread("chat-api", _THREAD)
    assert [item["message_id"] for item in read["items"]] == ["api-1"]
    assert "error" not in read["items"][0]


async def test_admin_reads_any_person_thread_on_a_valid_route(fake, monkeypatch):
    fake.seed_route("chat-api")
    store = _store()
    await store.create_record(
        _person_record("api-1", "chat-api", door="api", caller_principal="alice", created_at=10.0)
    )
    _seed_person(fake, _person([_addr(door="api", address="alice/u7", routes=["chat-api"])]))
    _wire_manager(monkeypatch, _api_route("chat-api"))
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    read = await ops.get_conversation_thread("chat-api", _THREAD)
    assert [item["message_id"] for item in read["items"]] == ["api-1"]


async def test_admin_unknown_route_keeps_its_own_404(fake, monkeypatch):
    _seed_person(fake, _person([_addr(door="api", address="alice/u7", routes=["chat-api"])]))
    _wire_manager(monkeypatch, _api_route("chat-api"))
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    with pytest.raises(NotFoundError, match="route not found"):
        await ops.get_conversation_thread("nope", _THREAD)


async def test_a_route_the_person_never_wrote_under_is_404(fake, monkeypatch):
    # ``other`` is a valid route on the same target, but not one of the person's routes:
    # governs authz only, so it 404s the same as an unknown thread.
    fake.seed_route("chat-api")
    _seed_person(fake, _person([_addr(door="api", address="alice/u7", routes=["chat-api"])]))
    _wire_manager(monkeypatch, _api_route("chat-api"), _api_route("other"))
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    with pytest.raises(NotFoundError, match="thread not found"):
        await ops.get_conversation_thread("other", _THREAD)


async def test_a_target_mismatch_is_404(fake, monkeypatch):
    # The route resolves to a DIFFERENT target than the person is scoped to.
    other_target = ConversationRoute(
        route_name="chat-api",
        door="api",
        target_kind="agent",
        target_name="other-agent",
        execution_key="svc",
        callback_url="https://cb.example/x",
        execution_key_fingerprint="fp",
    )
    _seed_person(fake, _person([_addr(door="api", address="alice/u7", routes=["chat-api"])]))
    _wire_manager(monkeypatch, other_target)
    _as_caller(monkeypatch, _Caller("root", is_admin=True))
    with pytest.raises(NotFoundError, match="thread not found"):
        await ops.get_conversation_thread("chat-api", _THREAD)
