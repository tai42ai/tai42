"""Bind a stub app before the plugin is imported.

``register`` registers the channel, inbound route, and startup hook on
``tai42_app`` at import time; a stub bound here (at collection, before any test
imports the plugin) satisfies those. Tests drive HTTP through
``httpx.MockTransport`` and the correlation store through ``FakeRedis``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from tai42_contract.app import tai42_app
from tai42_contract.channels import InboundAnswerOutcome, InboundAnswerResult
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import reset_all_settings


class _ClientCtx:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _StubClients:
    """Dispatches ``client_ctx`` by client class to test-set fakes."""

    def __init__(self) -> None:
        self.by_class: dict[type, Any] = {}

    def client_ctx(self, client_cls: type, settings: Any = None, *, fresh: bool = False, **kwargs: Any) -> _ClientCtx:
        client = self.by_class.get(client_cls)
        if client is None:
            raise RuntimeError(f"test must set a stub client for {client_cls.__name__}")
        return _ClientCtx(client)


class _StubChannels:
    """Records channel registrations and stands in for the shared inbound-answer
    ladder (``handle_inbound_answer``) the real skeleton exposes on ``app.channels`` —
    the plugin's test venv cannot import the skeleton, so the ladder is faked at this
    contract seam. A test sets the outcome (or an error) and reads back the call."""

    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}
        self.inbound_calls: list[SimpleNamespace] = []
        self.inbound_outcome: InboundAnswerOutcome = InboundAnswerOutcome.NO_CORRELATION
        self.inbound_error: BaseException | None = None

    def register(self, name: str, channel: Any) -> None:
        if name in self.registered:
            raise ValueError(f"channel {name!r} is already registered")
        self.registered[name] = channel

    def get(self, name: str) -> Any:
        return self.registered[name]

    def names(self) -> list[str]:
        return sorted(self.registered)

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
        return InboundAnswerResult(outcome=self.inbound_outcome)


class _StubHttp:
    def __init__(self) -> None:
        self.routes: list[SimpleNamespace] = []
        # The resolved absolute mount base register captures at import; a test
        # overrides it to prove setWebhook follows a remapped route base.
        self.mount_base_value = "/api/channels/telegram"

    def mount_base(self) -> str:
        return self.mount_base_value

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
        action: str | None = None,
    ):
        def decorator(handler):
            self.routes.append(
                SimpleNamespace(path=path, methods=methods, summary=summary, tags=tags, authed=authed, handler=handler)
            )
            return handler

        return decorator


class _StubLifecycle:
    def __init__(self) -> None:
        self.startup_hooks: list[Any] = []

    def on_startup(self, func):
        self.startup_hooks.append(func)
        return func

    def on_shutdown(self, func):
        return func

    def on_reload(self, func):
        return func


class _StubConversations:
    """Records bridge-facet calls; ``accept`` returns a message_id or raises a
    test-set error, ``record_delivery_status`` just records."""

    def __init__(self) -> None:
        self.accept_calls: list[SimpleNamespace] = []
        self.accept_result = "msg-0001"
        self.accept_error: BaseException | None = None
        self.status_calls: list[SimpleNamespace] = []

    async def accept(
        self, channel: str, our_identity: str, client_address: str, cap_key: str, text: str, provider_message_id: str
    ) -> str:
        self.accept_calls.append(
            SimpleNamespace(
                channel=channel,
                our_identity=our_identity,
                client_address=client_address,
                cap_key=cap_key,
                text=text,
                provider_message_id=provider_message_id,
            )
        )
        if self.accept_error is not None:
            raise self.accept_error
        return self.accept_result

    async def record_delivery_status(self, channel: str, provider_message_id: str, status: Any) -> None:
        self.status_calls.append(
            SimpleNamespace(channel=channel, provider_message_id=provider_message_id, status=status)
        )


class _StubApp:
    def __init__(self) -> None:
        self.channels = _StubChannels()
        self.http = _StubHttp()
        self.lifecycle = _StubLifecycle()
        self.clients = _StubClients()
        self.conversations = _StubConversations()


_stub_app = _StubApp()
tai42_app.bind(_stub_app)


class FakeRedis:
    """In-memory stand-in for the async Redis commands the correlation store uses.

    ``set`` honours ``nx`` (the store reserves NX for one-pending-per-anchor): a
    reserve on a held key returns ``None`` (redis-py's miss signal), else ``True``."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool | None:
        if nx and key in self.data:
            return None
        self.data[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)
        self.ttls.pop(key, None)


@pytest.fixture
def stub_app() -> _StubApp:
    return _stub_app


@pytest.fixture(autouse=True)
def _reset_conversations() -> Any:
    """Clear recorded bridge-facet and inbound-ladder calls so the singleton stub
    never leaks state across tests."""
    conv = _stub_app.conversations
    conv.accept_calls.clear()
    conv.status_calls.clear()
    conv.accept_result = "msg-0001"
    conv.accept_error = None
    channels = _stub_app.channels
    channels.inbound_calls.clear()
    channels.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION
    channels.inbound_error = None
    yield
    conv.accept_calls.clear()
    conv.status_calls.clear()
    conv.accept_error = None
    channels.inbound_calls.clear()
    channels.inbound_error = None


@pytest.fixture
def conversations() -> _StubConversations:
    return _stub_app.conversations


@pytest.fixture
def channels() -> _StubChannels:
    return _stub_app.channels


@pytest.fixture
def fake_redis(stub_app: _StubApp):
    redis = FakeRedis()
    stub_app.clients.by_class[RedisClient] = redis
    try:
        yield redis
    finally:
        stub_app.clients.by_class.pop(RedisClient, None)


@pytest.fixture
async def http_recorder(stub_app: _StubApp):
    """An httpx.AsyncClient over MockTransport; records requests, replies via a
    test-set responder (default: Telegram-shaped ok:true sendMessage result)."""
    state = SimpleNamespace(requests=[], responder=None)

    def default_responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        return (state.responder or default_responder)(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stub_app.clients.by_class[HttpxClient] = client
    try:
        yield state
    finally:
        stub_app.clients.by_class.pop(HttpxClient, None)
        await client.aclose()


@pytest.fixture(autouse=True)
def channel_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """A complete valid CHANNEL_TELEGRAM_* environment; tests override per case.

    Settings caches are reset around every test so env changes are seen. The
    ``integration``-marked live suite is exempt from the injected values: it
    reads the real ambient environment (a fixture credential there would point
    a live test at the real API with fake values) — only the cache reset applies.
    """
    if request.node.get_closest_marker("integration") is not None:
        reset_all_settings()
        yield
        reset_all_settings()
        return
    monkeypatch.setenv("CHANNEL_TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("CHANNEL_TELEGRAM_DEFAULT_RECIPIENT", "777")
    monkeypatch.setenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS", "888,999")
    monkeypatch.setenv("CHANNEL_TELEGRAM_WEBHOOK_SECRET", "s3cret_token")
    monkeypatch.setenv("CHANNEL_TELEGRAM_PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("CHANNEL_TELEGRAM_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    yield
    reset_all_settings()


def make_inbound_request(
    payload: Any = None,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
    chunks: list[bytes] | None = None,
) -> Request:
    """A real Starlette Request aimed at the inbound handler.

    ``chunks`` feeds the body as exactly that sequence of stream messages, so
    a test can prove the handler bounds the body WHILE streaming; the not-yet-
    pulled messages stay inspectable at
    ``request.scope["_pending_body_messages"]``.
    """
    if chunks is None:
        chunks = [raw if raw is not None else json.dumps(payload or {}).encode()]
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i + 1 < len(chunks)} for i, chunk in enumerate(chunks)
    ]
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/channels/telegram/inbound",
        "headers": raw_headers,
        "query_string": b"",
        "_pending_body_messages": messages,
    }

    async def receive():
        return messages.pop(0)

    return Request(scope, receive)
