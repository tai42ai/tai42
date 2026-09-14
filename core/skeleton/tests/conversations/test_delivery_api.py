"""Per-message api-door delivery: the REAL signed callback POST against a local httpx transport,
the retry/backoff under the delivery lease, and the poll-only route that delivers without a POST.

The signed callback runs the actual request the executor builds, so the body and header asserted
are the ones a receiver would recompute the HMAC over."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time

import httpx
import pytest
from tai42_contract.conversations import ConversationRoute

from tai42_skeleton.conversations import delivery as delivery_module
from tai42_skeleton.conversations import delivery_api as delivery_api_module
from tai42_skeleton.conversations import ledger as ledger_module
from tai42_skeleton.conversations import records as records_module
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.settings import ConversationsSettings

from .fake_record_redis import FakeRecordRedis, make_record_client_ctx

#: The channel width the shared record env declares; the api door never chunks, so this only
#: satisfies the ``CONVERSATIONS_MAX_MESSAGE_CHARS`` entry the settings model requires.
_CHUNK_CHARS = 10


@pytest.fixture(autouse=True)
def _conversations_env(monkeypatch):
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_CLAIM_LEASE_SECONDS", "120")
    monkeypatch.setenv("CONVERSATIONS_MAX_MESSAGE_CHARS", f'{{"twilio": {_CHUNK_CHARS}}}')


@pytest.fixture
def fake(monkeypatch) -> FakeRecordRedis:
    """One faked redis behind both the record store and the send ledger, so a test reads
    the same keyspace the executor writes through."""
    backing = FakeRecordRedis()
    monkeypatch.setattr(records_module, "client_ctx", make_record_client_ctx(backing))
    monkeypatch.setattr(ledger_module, "client_ctx", make_record_client_ctx(backing))
    return backing


@pytest.fixture
def store(fake: FakeRecordRedis) -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


def _claim(fake: FakeRecordRedis, message_id: str) -> tuple[str, float]:
    """The record's live lease as ``(token, expiry)``; an empty token when it is free."""
    raw = fake._hashes[ConversationsSettings().record_key(message_id)]["claim"]
    if not raw:
        return "", 0.0
    token, expiry = raw.split(":", 1)
    return token, float(expiry)


def _expire_claim(fake: FakeRecordRedis, message_id: str) -> None:
    """Age the record's lease out — what the passage of time does to a dead worker's."""
    key = ConversationsSettings().record_key(message_id)
    token = fake._hashes[key]["claim"].split(":", 1)[0]
    fake._hashes[key]["claim"] = f"{token}:{time.time() - 1}"


async def _get(store: ConversationRecordStore, message_id: str) -> ConversationRecord:
    record = await store.get_record(message_id)
    assert record is not None
    return record


_CALLBACK_URL = "https://cb.example/hook"
_CALLBACK_SECRET = "sec-1"


def _api_route() -> ConversationRoute:
    return ConversationRoute(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        callback_url=_CALLBACK_URL,
        callback_secret=_CALLBACK_SECRET,
        execution_key_fingerprint="fp-1",
    )


class _FakeRouteManager:
    def __init__(self, route: ConversationRoute) -> None:
        self._route = route

    async def get_route(self, name: str) -> ConversationRoute | None:
        return self._route if name == self._route.route_name else None


def _api_record(message_id: str, answer: str) -> ConversationRecord:
    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name="chat",
        door="api",
        thread_id="bridge:chat:alice/user-7",
        client_address="alice/user-7",
        caller_principal="alice",
        callback_url=_CALLBACK_URL,
        origin="client",
        inbound_text=f"ask {message_id}",
        answer_status="answered",
        answer=answer,
        created_at=now,
        updated_at=now,
    )


def _wire_callback(monkeypatch, handler) -> tuple[list[httpx.Request], list[dict]]:
    """Drive the REAL ``_post_callback`` against a local transport, so the request the
    executor actually builds is what is asserted. Returns the requests it made and the
    kwargs it constructed its client with."""
    monkeypatch.setattr(delivery_module, "get_conversations_manager", lambda: _FakeRouteManager(_api_route()))
    requests: list[httpx.Request] = []
    client_kwargs: list[dict] = []
    real_client = httpx.AsyncClient

    async def _handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return await handler(request)

    def _client(**kwargs):
        client_kwargs.append(kwargs)
        return real_client(transport=httpx.MockTransport(_handle), **kwargs)

    monkeypatch.setattr(delivery_api_module.httpx, "AsyncClient", _client)
    return requests, client_kwargs


async def test_a_callback_timeout_at_or_above_the_lease_is_refused(monkeypatch):
    """A callback POST bounded at or above the lease can still be in flight after the sweep
    has re-claimed the record, which re-POSTs the identical signed callback — a duplicate
    delivery to the external receiver."""
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_CALLBACK_TIMEOUT_SECONDS", "120")
    with pytest.raises(ValueError, match="DELIVERY_CALLBACK_TIMEOUT_SECONDS"):
        ConversationsSettings()


async def test_the_callback_post_is_signed_under_the_rows_secret(monkeypatch, fake, store):
    """The whole point of the api door: a receiver recomputes the HMAC over the RAW BODY it
    received and compares it against the header the executor sent."""
    await store.create_record(_api_record("m-api", "the answer"))
    record = await _get(store, "m-api")

    async def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    requests, client_kwargs = _wire_callback(monkeypatch, _ok)
    assert await store.claim_delivery("m-api", time.time(), "worker-1", 120) == 1
    await delivery_api_module._deliver_api(store, record, "worker-1")

    assert len(requests) == 1
    sent = requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == _CALLBACK_URL
    # The executor serializes with ``exclude_none=True`` (so a silent answer and a
    # single-message answer's null ``parts`` never ride the wire), and this asserts exactly
    # the body it built.
    assert sent.content == record.answer_payload().model_dump_json(exclude_none=True).encode()
    assert sent.headers["Content-Type"] == "application/json"

    signature = sent.headers["X-Tai-Signature"]
    expected = "sha256=" + hmac.new(_CALLBACK_SECRET.encode(), sent.content, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(signature, expected)
    forged = "sha256=" + hmac.new(b"wrong-secret", sent.content, hashlib.sha256).hexdigest()
    assert not hmac.compare_digest(signature, forged)

    # The callback is bounded and takes no proxy/CA configuration from the environment.
    assert client_kwargs == [{"timeout": ConversationsSettings().delivery_callback_timeout_seconds, "trust_env": False}]
    assert (await _get(store, "m-api")).delivery_status is DeliveryStatus.DELIVERED


async def test_the_callback_timeout_comes_from_settings(monkeypatch, fake, store):
    """The POST is bounded by ``delivery_callback_timeout_seconds``, not a hardcoded
    constant: an operator lowering it below the lease is honoured on the wire. Reverting
    ``_deliver_api`` to a fixed timeout leaves the client built with 15 and reddens this."""
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_CALLBACK_TIMEOUT_SECONDS", "7")
    store = ConversationRecordStore(ConversationsSettings())
    await store.create_record(_api_record("m-timeout", "the answer"))

    async def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    _, client_kwargs = _wire_callback(monkeypatch, _ok)
    assert await store.claim_delivery("m-timeout", time.time(), "worker-1", 120) == 1
    await delivery_api_module._deliver_api(store, await _get(store, "m-timeout"), "worker-1")

    assert client_kwargs == [{"timeout": 7.0, "trust_env": False}]
    assert (await _get(store, "m-timeout")).delivery_status is DeliveryStatus.DELIVERED


async def test_a_slow_callback_is_bounded_by_the_total_deadline_not_left_in_flight(monkeypatch, fake, store):
    """A receiver that answers slower than the timeout is cut off as a retryable non-2xx, so a
    POST cannot outlive the lease and a sweep re-claim cannot double-deliver. The bound is the
    total request time, not httpx's per-phase timeout: reverting to the plain httpx timeout (no
    asyncio.timeout wall) leaves this POST in flight and hangs the test past the deadline."""
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_CALLBACK_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_BASE_SECONDS", "0.01")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_MAX_SECONDS", "0.01")
    store = ConversationRecordStore(ConversationsSettings())
    await store.create_record(_api_record("m-slow", "the answer"))

    async def _slow_then_ok(request: httpx.Request) -> httpx.Response:
        if len(requests) == 1:
            await asyncio.sleep(5)  # far past the 0.05s total deadline
        return httpx.Response(200)

    requests, _ = _wire_callback(monkeypatch, _slow_then_ok)
    assert await store.claim_delivery("m-slow", time.time(), "worker-1", 120) == 1
    async with asyncio.timeout(2):  # the fix bounds the POST; without it this test would hang
        await delivery_api_module._deliver_api(store, await _get(store, "m-slow"), "worker-1")

    assert len(requests) == 2  # the slow first POST was cut off and retried
    assert (await _get(store, "m-slow")).delivery_status is DeliveryStatus.DELIVERED


async def test_a_transport_failure_is_retried_rather_than_raised(monkeypatch, fake, store):
    """A connection reset is a retryable non-2xx, not an exception that strands the record:
    the record must still reach a terminal state on the next attempt."""
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_BASE_SECONDS", "0.01")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_MAX_SECONDS", "0.01")
    store = ConversationRecordStore(ConversationsSettings())
    await store.create_record(_api_record("m-blip", "the answer"))

    async def _reset_then_accept(request: httpx.Request) -> httpx.Response:
        if len(requests) == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200)

    requests, _ = _wire_callback(monkeypatch, _reset_then_accept)
    assert await store.claim_delivery("m-blip", time.time(), "worker-1", 120) == 1
    await delivery_api_module._deliver_api(store, await _get(store, "m-blip"), "worker-1")

    assert len(requests) == 2
    assert (await _get(store, "m-blip")).delivery_status is DeliveryStatus.DELIVERED


async def test_the_api_retry_extends_the_lease_over_its_own_backoff(monkeypatch, fake, store):
    """The backoff is dead time the record must stay claimed through, so the refresh leases
    it for the backoff PLUS a full lease — a plain lease could lapse mid-sleep."""
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_BASE_SECONDS", "0.5")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_MAX_SECONDS", "0.5")
    settings = ConversationsSettings()
    store = ConversationRecordStore(settings)
    await store.create_record(_api_record("m-backoff", "the answer"))
    observed: list[float] = []

    async def _refuse_then_accept(request: httpx.Request) -> httpx.Response:
        if len(requests) == 1:
            observed.append(time.time())
            return httpx.Response(500)
        observed.append(_claim(fake, "m-backoff")[1])
        return httpx.Response(200)

    requests, _ = _wire_callback(monkeypatch, _refuse_then_accept)
    assert await store.claim_delivery("m-backoff", time.time(), "worker-1", 120) == 1
    await delivery_api_module._deliver_api(store, await _get(store, "m-backoff"), "worker-1")

    first_post, expiry = observed
    assert expiry >= first_post + 0.5 + settings.delivery_claim_lease_seconds


async def test_the_api_retry_stops_when_it_loses_the_lease_during_backoff(monkeypatch, fake, store):
    """Another worker takes the record over while this one is holding a 500. It must not
    wake up and POST a SECOND callback the caller's endpoint has already been sent."""
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_BASE_SECONDS", "0.01")
    monkeypatch.setenv("CONVERSATIONS_DELIVERY_BACKOFF_MAX_SECONDS", "0.01")
    store = ConversationRecordStore(ConversationsSettings())
    await store.create_record(_api_record("m-taken", "the answer"))

    async def _refuse_and_hand_over(request: httpx.Request) -> httpx.Response:
        _expire_claim(fake, "m-taken")
        assert await store.claim_delivery("m-taken", time.time(), "worker-2", 120) == 1
        return httpx.Response(500)

    requests, _ = _wire_callback(monkeypatch, _refuse_and_hand_over)
    assert await store.claim_delivery("m-taken", time.time(), "worker-1", 120) == 1
    await delivery_api_module._deliver_api(store, await _get(store, "m-taken"), "worker-1")

    assert len(requests) == 1
    assert (await _get(store, "m-taken")).delivery_status is DeliveryStatus.PENDING_DELIVERY
    assert _claim(fake, "m-taken")[0] == "worker-2"


def _no_callback_api_route() -> ConversationRoute:
    return ConversationRoute(
        route_name="chat",
        door="api",
        target_kind="agent",
        target_name="echo",
        execution_key="svc",
        callback_url=None,
        callback_secret=None,
        execution_key_fingerprint="fp-1",
    )


def _no_callback_api_record(message_id: str, answer: str) -> ConversationRecord:
    now = time.time()
    return ConversationRecord(
        message_id=message_id,
        route_name="chat",
        door="api",
        thread_id="bridge:chat:alice/user-7",
        client_address="alice/user-7",
        caller_principal="alice",
        callback_url=None,
        origin="client",
        inbound_text=f"ask {message_id}",
        answer_status="answered",
        answer=answer,
        created_at=now,
        updated_at=now,
    )


async def test_a_no_callback_api_record_delivers_readable_without_a_post(monkeypatch, fake, store):
    """An api route that declares no callback serves its answer from the poll door, so
    delivery is terminal the moment the outcome is written: the record goes DELIVERED, no
    callback is POSTed, and no send attempt is spent."""
    await store.create_record(_no_callback_api_record("m-poll", "the answer"))
    posted: list = []

    async def _post(url, body, signature, timeout_seconds):
        posted.append(url)
        return 200

    monkeypatch.setattr(
        delivery_module, "get_conversations_manager", lambda: _FakeRouteManager(_no_callback_api_route())
    )
    monkeypatch.setattr(delivery_module, "_post_callback", _post)
    assert await store.claim_delivery("m-poll", time.time(), "worker-1", 120) == 1
    await delivery_api_module._deliver_api(store, await _get(store, "m-poll"), "worker-1")

    assert posted == []  # no callback attempt
    delivered = await _get(store, "m-poll")
    assert delivered.delivery_status is DeliveryStatus.DELIVERED
    assert delivered.attempts == 0  # no send attempt was spent
    # The poll door reads the answer back off the terminal record.
    assert delivered.caller_view()["answer"] == "the answer"
