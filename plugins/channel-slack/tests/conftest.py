"""Bind a light stub app before the plugin is imported, and provide the shared
fakes/helpers.

``inbound`` registers its route and ``register`` registers the channel on
``tai42_app`` at import time, and outbound calls reach clients via
``tai42_app.clients.client_ctx``; a stub bound here (at collection, before any
test imports the plugin) satisfies all three. Tests wire clients per class: an
``httpx.AsyncClient`` over a scripted ``MockTransport`` under ``HttpxClient`` and
a :class:`FakeRedis` under ``RedisClient``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from starlette.requests import Request
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDelivery, InboundAnswerOutcome, InboundAnswerResult
from tai42_contract.interactions.models import (
    MEDIA_ROUTE_PREFIX,
    IngestedMedia,
    MediaItem,
    MediaKind,
    MediaOrigin,
)
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import reset_all_settings

TEST_SIGNING_SECRET = "test-signing-secret"
TEST_BOT_TOKEN = "xoxb-test-token"
TEST_DEFAULT_RECIPIENT = "C0TESTCHAN"
TEST_ALLOWED_RECIPIENT = "C0ALLOWED1"
TEST_BOT_USER_ID = "U0BOTSELF"

_ENV_VARS = (
    "CHANNEL_SLACK_BOT_TOKEN",
    "CHANNEL_SLACK_SIGNING_SECRET",
    "CHANNEL_SLACK_BOT_USER_ID",
    "CHANNEL_SLACK_ALLOWED_RECIPIENTS",
    "CHANNEL_SLACK_DEFAULT_RECIPIENT",
    "CHANNEL_SLACK_API_BASE_URL",
    "CHANNEL_SLACK_REDIS_URL",
    "CHANNEL_SLACK_HTTP_TIMEOUT_SECONDS",
)


class _ClientCtx:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _StubClients:
    """Dispatches ``client_ctx`` on the client class to a test-set instance."""

    def __init__(self) -> None:
        self.clients: dict[type, Any] = {}

    def client_ctx(self, client_cls: type, settings: Any = None, *, fresh: bool = False, **kwargs: Any) -> _ClientCtx:
        if client_cls not in self.clients:
            raise RuntimeError(f"test must set stub client for {client_cls.__name__}")
        return _ClientCtx(self.clients[client_cls])


class _StubChannels:
    """Records channel registrations and stands in for the shared inbound-answer ladder
    the real skeleton exposes on ``app.channels`` — the plugin's test venv cannot import
    the skeleton, so the ladder is faked at this contract seam. A test sets the outcome
    (or an error) and reads back the recorded call; a terminal outcome mirrors the
    ladder's ONE store side-effect (release) so store-state assertions hold."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}
        self.inbound_calls: list[SimpleNamespace] = []
        self.inbound_outcome: InboundAnswerOutcome = InboundAnswerOutcome.NO_CORRELATION
        self.inbound_retry_reason: str | None = None
        self.inbound_retry_field: str | None = None
        self.inbound_error: BaseException | None = None

    def reset(self) -> None:
        self.inbound_calls.clear()
        self.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION
        self.inbound_retry_reason = None
        self.inbound_retry_field = None
        self.inbound_error = None

    def register(self, name: str, channel: Any) -> None:
        # A real app raises on a duplicate name; the capture mirrors that so a
        # double-registration bug surfaces in tests rather than being masked.
        if name in self.registered:
            raise ValueError(f"channel {name!r} is already registered")
        self.registered[name] = channel

    async def handle_inbound_answer(
        self, *, channel_id: str, correlation_key: str, answer: Any, store: Any, bridge: Any
    ) -> InboundAnswerResult:
        self.inbound_calls.append(
            SimpleNamespace(
                channel_id=channel_id, correlation_key=correlation_key, answer=answer, store=store, bridge=bridge
            )
        )
        if self.inbound_error is not None:
            raise self.inbound_error
        if self.inbound_outcome in (InboundAnswerOutcome.FORWARDED, InboundAnswerOutcome.BRIDGED):
            await store.release_correlation(correlation_key)
        return InboundAnswerResult(
            outcome=self.inbound_outcome,
            retry_reason=self.inbound_retry_reason,
            retry_field=self.inbound_retry_field,
        )


class _StubInteractions:
    """Stands in for the skeleton's ``app.interactions`` reaction chokepoint.

    The plugin's test venv cannot import the skeleton that hosts the real ``react`` facet, so
    it is faked at this contract seam. A test sets ``react_result`` (a form update) or
    ``react_error`` (raised, standing in for a closed/malformed/handler failure) and reads back
    the recorded ``react`` calls."""

    def __init__(self) -> None:
        self.react_calls: list[SimpleNamespace] = []
        self.react_result: dict[str, Any] = {}
        self.react_error: BaseException | None = None

    def reset(self) -> None:
        self.react_calls.clear()
        self.react_result = {}
        self.react_error = None

    async def react(self, interaction_id: str, event: dict[str, Any], partial_values: dict[str, Any]) -> dict[str, Any]:
        self.react_calls.append(
            SimpleNamespace(interaction_id=interaction_id, event=event, partial_values=partial_values)
        )
        if self.react_error is not None:
            raise self.react_error
        return self.react_result


class _StubHttp:
    """Records every route registered through ``tai42_app.http.custom_route``."""

    def __init__(self) -> None:
        self.routes: dict[str, SimpleNamespace] = {}

    def custom_route(
        self,
        path: str,
        methods: list[str],
        name: str | None = None,
        include_in_schema: bool = True,
        *,
        summary: str,
        tags: list[str],
        response_model: Any,
        request_model: Any = None,
        query_model: Any = None,
        authed: bool | None = None,
        destructive: bool = False,
        action: str | None = None,
        declared: Any = None,
        no_body_reason: str | None = None,
    ) -> Any:
        def decorator(handler: Any) -> Any:
            self.routes[path] = SimpleNamespace(
                handler=handler,
                methods=methods,
                authed=authed,
                summary=summary,
                tags=tags,
            )
            return handler

        return decorator


class _StubConversations:
    """Records ``accept`` / ``record_delivery_status`` calls the bridge makes.

    ``accept`` returns ``accept_result`` unless ``accept_error`` is set, in which
    case it raises it — a ``LookupError`` stands in for the no-route refusal, any
    other exception for an infrastructure failure the adapter must propagate.
    """

    def __init__(self) -> None:
        self.accept_calls: list[SimpleNamespace] = []
        self.accept_result = "conv-msg-1"
        self.accept_error: BaseException | None = None
        self.status_calls: list[SimpleNamespace] = []
        self.rejected_calls: list[SimpleNamespace] = []

    def reset(self) -> None:
        self.accept_calls.clear()
        self.accept_result = "conv-msg-1"
        self.accept_error = None
        self.status_calls.clear()
        self.rejected_calls.clear()

    async def accept(
        self,
        channel: str,
        our_identity: str,
        client_address: str,
        cap_key: str,
        text: str,
        provider_message_id: str,
        params: dict[str, str] | None = None,
        attachments: list[MediaItem] | None = None,
    ) -> str:
        self.accept_calls.append(
            SimpleNamespace(
                channel=channel,
                our_identity=our_identity,
                client_address=client_address,
                cap_key=cap_key,
                text=text,
                provider_message_id=provider_message_id,
                params=params,
                attachments=attachments,
            )
        )
        if self.accept_error is not None:
            raise self.accept_error
        return self.accept_result

    async def record_delivery_status(self, channel: str, provider_message_id: str, status: Any) -> None:
        self.status_calls.append(
            SimpleNamespace(channel=channel, provider_message_id=provider_message_id, status=status)
        )

    async def notify_inbound_rejected(
        self, *, channel_id: str, recipient: str, sender_identity: str | None, kind: str, reason: Any
    ) -> None:
        self.rejected_calls.append(
            SimpleNamespace(
                channel_id=channel_id,
                recipient=recipient,
                sender_identity=sender_identity,
                kind=kind,
                reason=reason,
            )
        )


class _StubMedia:
    """Stands in for the served-media ingestion facet (``app.media``).

    The plugin's test venv cannot import the skeleton that hosts the real chokepoint, so it
    is faked at this contract seam. Each ``ingest_media`` call drains the streamed ``source``
    (mirroring the real per-chunk read), records the arguments, then returns the next queued
    :class:`IngestedMedia` — or raises the next queued error — so a test drives success and
    every failure class. ``ingest_default`` answers when the queue is empty; a dry queue with
    no default is a loud test error, never a silent stand-in.
    """

    def __init__(self) -> None:
        self.ingest_calls: list[SimpleNamespace] = []
        self.ingest_results: list[IngestedMedia | BaseException] = []
        self.ingest_default: IngestedMedia | None = None

    def reset(self) -> None:
        self.ingest_calls.clear()
        self.ingest_results.clear()
        self.ingest_default = None

    async def ingest_media(
        self,
        *,
        source: AsyncIterator[bytes],
        kind_hint: Any,
        declared_mime: str | None,
        filename: str | None,
        declared_size: int | None,
        origin: MediaOrigin,
    ) -> IngestedMedia:
        chunks = [chunk async for chunk in source]
        self.ingest_calls.append(
            SimpleNamespace(
                chunks=chunks,
                kind_hint=kind_hint,
                declared_mime=declared_mime,
                filename=filename,
                declared_size=declared_size,
                origin=origin,
            )
        )
        if self.ingest_results:
            result = self.ingest_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        if self.ingest_default is not None:
            return self.ingest_default
        raise AssertionError("no ingest result configured for this call")


class _StubLifecycle:
    """Records ``on_startup`` hooks so a test can drive the boot config guard."""

    def __init__(self) -> None:
        self.startup_hooks: list[Any] = []

    def on_startup(self, func: Any) -> Any:
        self.startup_hooks.append(func)
        return func

    def on_shutdown(self, func: Any) -> Any:
        return func


class _StubApp:
    def __init__(self) -> None:
        self.channels = _StubChannels()
        self.http = _StubHttp()
        self.clients = _StubClients()
        self.conversations = _StubConversations()
        self.media = _StubMedia()
        self.lifecycle = _StubLifecycle()
        self.interactions = _StubInteractions()


_stub_app = _StubApp()
# Bind at import time so the first import of any plugin module (its
# module-level registration side-effects) has an app to register against.
tai42_app.bind(_stub_app)


class FakeRedis:
    """get/set/delete with NX + EX recording — just enough redis for the store."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def delete(self, key: str) -> int:
        self.ttls.pop(key, None)
        return int(self.store.pop(key, None) is not None)


class ScriptedHttp:
    """A ``MockTransport`` script: records every request, pops queued results.

    Queue an ``httpx.Response`` to answer a request or an exception instance to
    raise it as a transport failure. Running dry is a loud test error, never a
    silent default response.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.results: list[httpx.Response | Exception] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.results:
            raise AssertionError(f"no scripted response left for {request.url}")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def stub_app() -> _StubApp:
    return _stub_app


@pytest.fixture
def stub_conversations() -> Iterator[_StubConversations]:
    """The bridge facet stub, state reset before and after so accept-call
    assertions never leak across tests."""
    conversations = _stub_app.conversations
    conversations.reset()
    try:
        yield conversations
    finally:
        conversations.reset()


@pytest.fixture
def stub_media() -> Iterator[_StubMedia]:
    """The served-media ingestion stub on ``app.media``, state reset around each test so
    ingest-call and result-queue assertions never leak."""
    media = _stub_app.media
    media.reset()
    try:
        yield media
    finally:
        media.reset()


@pytest.fixture
def channels() -> Iterator[_StubChannels]:
    """The inbound-answer-ladder stub on ``app.channels``, state reset around each
    test so outcome/call assertions never leak."""
    stub = _stub_app.channels
    stub.reset()
    try:
        yield stub
    finally:
        stub.reset()


@pytest.fixture
def interactions() -> Iterator[_StubInteractions]:
    """The reaction-chokepoint stub on ``app.interactions``, state reset around each
    test so react-call/result assertions never leak."""
    stub = _stub_app.interactions
    stub.reset()
    try:
        yield stub
    finally:
        stub.reset()


@pytest.fixture
def fake_redis() -> Iterator[FakeRedis]:
    """A :class:`FakeRedis` wired into ``client_ctx`` under ``RedisClient``."""
    redis = FakeRedis()
    _stub_app.clients.clients[RedisClient] = redis
    try:
        yield redis
    finally:
        _stub_app.clients.clients.pop(RedisClient, None)


@pytest.fixture
async def http_script() -> AsyncIterator[ScriptedHttp]:
    """A scripted ``httpx.AsyncClient`` wired into ``client_ctx`` under
    ``HttpxClient``; outbound payloads are asserted from ``.requests``."""
    script = ScriptedHttp()
    client = httpx.AsyncClient(transport=httpx.MockTransport(script.handler))
    _stub_app.clients.clients[HttpxClient] = client
    try:
        yield script
    finally:
        _stub_app.clients.clients.pop(HttpxClient, None)
        await client.aclose()


@pytest.fixture
def slack_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The ``CHANNEL_SLACK_*`` env vars set (credentials, one allowlisted
    recipient, a default recipient, the store URL), settings caches reset
    before and after — cached settings never leak across tests."""
    monkeypatch.setenv("CHANNEL_SLACK_BOT_TOKEN", TEST_BOT_TOKEN)
    monkeypatch.setenv("CHANNEL_SLACK_SIGNING_SECRET", TEST_SIGNING_SECRET)
    monkeypatch.setenv("CHANNEL_SLACK_BOT_USER_ID", TEST_BOT_USER_ID)
    monkeypatch.setenv("CHANNEL_SLACK_ALLOWED_RECIPIENTS", TEST_ALLOWED_RECIPIENT)
    monkeypatch.setenv("CHANNEL_SLACK_DEFAULT_RECIPIENT", TEST_DEFAULT_RECIPIENT)
    monkeypatch.setenv("CHANNEL_SLACK_REDIS_URL", "redis://correlation-store:6379/0")
    reset_all_settings()
    try:
        yield
    finally:
        reset_all_settings()


@pytest.fixture
def no_slack_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ``CHANNEL_SLACK_*`` env at all, settings caches reset before and
    after — the unconfigured/fail-closed branches."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    reset_all_settings()
    try:
        yield
    finally:
        reset_all_settings()


def make_delivery(
    answer_format: str = "text",
    options: list[str] | None = None,
    timeout_at: datetime | None = None,
    callback_url: str = "http://gateway/api/interactions/callback/ticket-1",
    recipient: str | None = None,
    schema: dict[str, Any] | None = None,
    interaction_id: str = "int-1",
    media: list[MediaItem] | None = None,
    data: Any = None,
    pages: Any = None,
) -> ChannelDelivery:
    """A valid ``ChannelDelivery`` with a comfortably-future default budget."""
    return ChannelDelivery(
        interaction_id=interaction_id,
        recipient=recipient,
        question="Deploy to production?",
        answer_format=answer_format,
        options=options,
        schema=schema,
        data=data,
        pages=pages,
        media=media,
        callback_url=callback_url,
        timeout_at=timeout_at or (datetime.now(UTC) + timedelta(minutes=10)),
    )


def make_interactive_body(payload: dict[str, Any]) -> bytes:
    """A Slack interactivity POST body: ``application/x-www-form-urlencoded`` with
    the JSON envelope in the ``payload`` field (what the signature covers)."""
    return urlencode({"payload": json.dumps(payload)}).encode()


def make_request(body: bytes, headers: dict[str, str]) -> Request:
    """A starlette ``Request`` over a raw ASGI scope carrying ``body`` — drives
    the captured route handler directly."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/channels/slack/inbound",
        "query_string": b"",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
    }
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def body_json(response: Any) -> Any:
    """The response body decoded as JSON (``Response.body`` may be a memoryview)."""
    return json.loads(bytes(response.body))


def signed_headers(body: bytes, secret: str, timestamp: int | None = None) -> dict[str, str]:
    """A valid ``X-Slack-Request-Timestamp`` + ``X-Slack-Signature`` pair for
    ``body`` (defaults to now), so flow tests go through REAL verification."""
    ts = int(time.time()) if timestamp is None else timestamp
    base = b"v0:" + str(ts).encode("ascii") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": str(ts),
        "X-Slack-Signature": f"v0={digest}",
    }


def _ok_response(ts: str | None = "1712345678.000100") -> httpx.Response:
    body: dict[str, object] = {"ok": True}
    if ts is not None:
        body["ts"] = ts
    return httpx.Response(200, json=body)


def make_ingested(
    *,
    media_id: str | None = None,
    kind: MediaKind = MediaKind.IMAGE,
    mime: str = "image/png",
    size: int = 2048,
    sha256: str = "a" * 64,
    filename: str | None = None,
    pending: bool = False,
) -> IngestedMedia:
    """An :class:`IngestedMedia` a stubbed ``ingest_media`` returns.

    ``media_id`` is the SERVED id (a valid 43-char token minted fresh when unset, so each
    call is distinct); ``item`` carries the served route url and, for a document, the
    SANITISED filename — set ``filename`` distinct from a test's raw vendor name to prove the
    bridge sources the sanitised value, never the raw one. Assert against the returned
    ``media_id`` / ``item.url``, not a literal.
    """
    resolved_id = media_id if media_id is not None else secrets.token_urlsafe(32)
    item = MediaItem(
        kind=kind,
        url=f"{MEDIA_ROUTE_PREFIX}{resolved_id}",
        caption=None,
        filename=filename if kind is MediaKind.DOCUMENT else None,
    )
    return IngestedMedia(item=item, media_id=resolved_id, size=size, sha256=sha256, mime=mime, pending=pending)


class FakeStream:
    """A :class:`~tai42_kit.net.MediaStream`-shaped object a fake ``open_media_stream`` yields."""

    def __init__(
        self,
        *,
        content_type: str | None = "image/png",
        content_length: int | None = None,
        host: str = "files.slack.com",
        chunks: tuple[bytes, ...] = (b"\x89PNG\r\n\x1a\n", b"payload"),
    ) -> None:
        self.content_type = content_type
        self.content_length = content_length
        self.host = host
        self._chunks = chunks

    @property
    def chunks(self) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            for chunk in self._chunks:
                yield chunk

        return _gen()


def fake_open_media_stream(
    stream: FakeStream | None = None,
    *,
    error: BaseException | None = None,
    calls: list[SimpleNamespace] | None = None,
) -> Any:
    """A stand-in for ``events.open_media_stream`` (patch it at that import site).

    Records each open into ``calls`` (url, headers, auth, follow_redirects), then raises
    ``error`` at open when set, else yields ``stream``.
    """
    resolved = stream if stream is not None else FakeStream()

    @asynccontextmanager
    async def _open(
        url: str,
        *,
        headers: Any = None,
        auth: Any = None,
        follow_redirects: bool = False,
    ) -> AsyncIterator[FakeStream]:
        if calls is not None:
            calls.append(SimpleNamespace(url=url, headers=headers, auth=auth, follow_redirects=follow_redirects))
        if error is not None:
            raise error
        yield resolved

    return _open
