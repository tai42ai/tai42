"""The thread read doors' HTTP edge: the paging query guard and the transcript's
``?thread_id=``.

An api-door thread id carries the ``{principal}/{end user}`` address, and the principal is
percent-encoded — so the id round-trips only as a query value: sent raw in a path the server
decodes it before routing, sent encoded the access-control path canonicalizer reads it as a
doubly-encoded byte. The operations themselves are covered in ``test_read_doors``.
"""

from __future__ import annotations

import importlib
import time
from urllib.parse import quote

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.settings import ConversationsSettings

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx
from .test_read_doors import _Caller, _DictManager

#: An OIDC-minted principal — the case a path-borne id cannot carry at all.
_PRINCIPAL = "oidc:google:1234"
_THREAD = f"bridge:chat:{quote(_PRINCIPAL, safe='')}/user-7"


def _router():
    from tai42_contract.app import tai42_app

    from tai42_skeleton.app.instance import app as skeleton_app

    with tai42_app.bound(skeleton_app):
        from tai42_skeleton.routers import conversations as router

    return router


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:6379/0")
    # An operation leaf is popped and re-imported by the reload suites, so both seams are
    # patched where the REGISTERED handler resolves them: the operation function's own
    # globals, and the records module sys.modules currently holds (what the operation body
    # imports at call time).
    fake = FakeRecordRedis()
    # The create writes the thread indexes only while the route still routes.
    fake.seed_route("chat")
    monkeypatch.setattr(
        importlib.import_module("tai42_skeleton.conversations.records"),
        "client_ctx",
        make_record_client_ctx(fake),
    )
    router = _router()

    async def _resolve():
        return _Caller("root", is_admin=True)

    op_globals = router._list_conversation_threads_op.__globals__
    monkeypatch.setitem(op_globals, "get_conversations_manager", lambda: _DictManager())
    monkeypatch.setitem(op_globals, "resolve_caller", _resolve)
    routes = [
        Route("/api/conversations/{route_name}/threads", router.list_conversation_threads, methods=["GET"]),
        Route("/api/conversations/{route_name}/transcript", router.get_conversation_thread, methods=["GET"]),
    ]
    return TestClient(Starlette(routes=routes))


async def _seed() -> None:
    """One api-door record, written through the store the doors read."""
    store_cls = importlib.import_module("tai42_skeleton.conversations.records").ConversationRecordStore
    now = time.time()
    await store_cls(ConversationsSettings()).create_record(
        ConversationRecord(
            message_id="t0",
            route_name="chat",
            door="api",
            thread_id=_THREAD,
            client_address=f"{quote(_PRINCIPAL, safe='')}/user-7",
            caller_principal=_PRINCIPAL,
            callback_url="https://cb.example/x",
            inbound_text="ask t0",
            answer_status="answered",
            answer="the answer",
            delivery_status=DeliveryStatus.DELIVERED,
            created_at=now,
            updated_at=now,
        )
    )


async def test_a_thread_id_holding_an_encoded_principal_round_trips_the_query(client):
    await _seed()

    response = client.get("/api/conversations/chat/transcript", params={"thread_id": _THREAD})

    assert response.status_code == 200
    assert [item["message_id"] for item in response.json()["data"]["items"]] == ["t0"]


async def test_the_transcript_serves_the_tail_order(client):
    await _seed()

    response = client.get("/api/conversations/chat/transcript", params={"thread_id": _THREAD, "order": "desc"})

    assert response.status_code == 200
    assert response.json()["data"]["order"] == "desc"


async def test_a_missing_thread_id_is_a_loud_400(client):
    response = client.get("/api/conversations/chat/transcript")

    assert response.status_code == 400
    assert "thread_id is required" in response.json()["error"]


async def test_a_blank_thread_id_is_a_loud_400(client):
    response = client.get("/api/conversations/chat/transcript", params={"thread_id": ""})

    assert response.status_code == 400
    assert "non-blank" in response.json()["error"]


async def test_an_unknown_order_is_a_loud_400(client):
    await _seed()

    response = client.get("/api/conversations/chat/transcript", params={"thread_id": _THREAD, "order": "newest"})

    assert response.status_code == 400
    assert "order must be one of" in response.json()["error"]


async def test_the_thread_listing_answers_the_envelope(client):
    await _seed()

    response = client.get("/api/conversations/chat/threads?page=1&pageSize=1")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"][0]["thread_id"] == _THREAD
    assert body["page"] == 1
    assert body["page_size"] == 1


@pytest.mark.parametrize("query", ["?page=nope", "?pageSize=many"])
async def test_a_non_integer_page_is_a_loud_400(client, query):
    response = client.get(f"/api/conversations/chat/threads{query}")

    assert response.status_code == 400
    assert "must be integers" in response.json()["error"]


async def test_a_page_below_one_is_a_loud_400(client):
    response = client.get("/api/conversations/chat/threads?page=0")

    assert response.status_code == 400
    assert "must be >= 1" in response.json()["error"]


async def test_a_page_past_the_maximum_is_a_loud_400(client):
    response = client.get(f"/api/conversations/chat/threads?page={2**62}")

    assert response.status_code == 400
    assert "page must be <=" in response.json()["error"]
