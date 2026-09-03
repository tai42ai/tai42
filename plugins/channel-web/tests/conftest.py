"""Bind a light stub app before the plugin is imported.

``register``/``routes`` register on ``tai42_app`` at import time and the store/
answer paths reach clients via ``tai42_app.clients.client_ctx``; a stub bound here
(at collection, before any test imports the plugin) satisfies all three.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from starlette.requests import Request
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDelivery, ChannelNotification
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import reset_all_settings

from tai42_channel_web import page, stream
from tai42_channel_web.session import session_cookie_name


class _ClientCtx:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _StubClients:
    def __init__(self) -> None:
        self.by_class: dict[type, Any] = {}
        self.ctx_kwargs: list[dict[str, Any]] = []

    def client_ctx(self, client_cls: type, settings: Any = None, **kwargs: Any) -> _ClientCtx:
        self.ctx_kwargs.append({"client_cls": client_cls, "settings": settings, **kwargs})
        if client_cls not in self.by_class:
            raise RuntimeError(f"test must set a stub client for {client_cls.__name__}")
        return _ClientCtx(self.by_class[client_cls])


class _StubChannels:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def register(self, name: str, channel: Any) -> None:
        if name in self.registered:
            raise ValueError(f"channel {name!r} already registered")
        self.registered[name] = channel


class _StubHttp:
    def __init__(self) -> None:
        self.routes: list[SimpleNamespace] = []

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
    ) -> Callable[[Any], Any]:
        # ``action`` is recorded as metadata only — the stub enforces nothing (the
        # fail-closed authed-without-action refusal is the core registry's, exercised
        # by the skeleton route-parity gate, not here).
        def decorator(fn: Any) -> Any:
            self.routes.append(
                SimpleNamespace(
                    path=path,
                    methods=methods,
                    authed=authed,
                    action=action,
                    summary=summary,
                    tags=tags,
                    handler=fn,
                )
            )
            return fn

        return decorator


class _StubConversations:
    """Records ``accept`` calls and replays a queued result or raises a queued
    exception, so the message-door tests assert the exact facet args and drive the
    blank/route/overflow outcomes."""

    def __init__(self) -> None:
        self.accept_calls: list[dict[str, Any]] = []
        self.accept_result: str = "msg-stub-id"
        self.accept_error: Exception | None = None

    async def accept(
        self,
        channel: str,
        our_identity: str,
        client_address: str,
        cap_key: str,
        text: str,
        provider_message_id: str,
        params: dict[str, str] | None = None,
        form: dict[str, Any] | None = None,
    ) -> str:
        self.accept_calls.append(
            {
                "channel": channel,
                "our_identity": our_identity,
                "client_address": client_address,
                "cap_key": cap_key,
                "text": text,
                "provider_message_id": provider_message_id,
                "params": params,
                "form": form,
            }
        )
        if self.accept_error is not None:
            raise self.accept_error
        return self.accept_result


class _StubApp:
    def __init__(self) -> None:
        self.channels = _StubChannels()
        self.clients = _StubClients()
        self.http = _StubHttp()
        self.conversations = _StubConversations()


_stub_app = _StubApp()

# Whatever the shared handle held before this conftest imported (nothing in an isolated
# run; the real app when another package's suite bound it first). Captured BEFORE the
# module-level bind below so the session finalizer can hand it back — the stub never
# leaks onto the shared handle into a cross-package run.
_pre_import_impl = cast("Any", tai42_app)._impl

# ``register``/``routes`` register on ``tai42_app`` at import time and test_routes
# collects handlers off the stub, both BEFORE any test runs — so the stub is bound at
# import here, not merely in a fixture. Per-test rebinding and the suite-end restore are
# the fixtures below.
tai42_app.bind(_stub_app)


class FakeHttpx:
    """Records every ``post`` call and replays queued responses (or raises queued exceptions)."""

    def __init__(self, events: list[tuple[str, str]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[httpx.Response | Exception] = []
        self.events = events if events is not None else []

    async def post(self, url: str, *, json: Any = None, headers: dict[str, str] | None = None) -> httpx.Response:
        self.events.append(("http_post", url))
        self.calls.append({"url": url, "json": json, "headers": headers})
        if not self.responses:
            raise AssertionError("FakeHttpx: no queued response for post")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _parse_id(entry_id: str) -> tuple[int, int]:
    a, _, b = entry_id.partition("-")
    return int(a), int(b or 0)


def _entry_gt(entry_id: str, cursor: str) -> bool:
    """Stream id ordering for ``xread``: entries strictly after ``cursor``."""
    return _parse_id(entry_id) > _parse_id(cursor)


def _in_range(entry_id: str, low: str, high: str) -> bool:
    """``XRANGE`` bounds — inclusive, with ``-``/``+`` for the open ends."""
    parsed = _parse_id(entry_id)
    if low != "-" and parsed < _parse_id(low):
        return False
    return high == "+" or parsed <= _parse_id(high)


def _redis_glob_match(pattern: str, value: str) -> bool:
    """Redis KEYS/SCAN glob semantics for the fake ``scan_iter`` — ``*`` (any run),
    ``?`` (one char), ``[set]`` (with ``^`` negation and ``a-b`` ranges), and ``\\``
    escaping the next metacharacter. Ignoring ``MATCH`` would make the glob-escape
    test pass vacuously, so the fake honors it exactly."""

    def match(pi: int, vi: int) -> bool:
        while pi < len(pattern):
            char = pattern[pi]
            if char == "*":
                while pi < len(pattern) and pattern[pi] == "*":
                    pi += 1
                if pi == len(pattern):
                    return True
                return any(match(pi, vk) for vk in range(vi, len(value) + 1))
            if vi >= len(value):
                return False
            if char == "?":
                pi, vi = pi + 1, vi + 1
                continue
            if char == "[":
                pi += 1
                negate = pi < len(pattern) and pattern[pi] == "^"
                if negate:
                    pi += 1
                hit = False
                while pi < len(pattern) and pattern[pi] != "]":
                    if pattern[pi] == "\\" and pi + 1 < len(pattern):
                        if value[vi] == pattern[pi + 1]:
                            hit = True
                        pi += 2
                    elif pi + 2 < len(pattern) and pattern[pi + 1] == "-" and pattern[pi + 2] != "]":
                        if pattern[pi] <= value[vi] <= pattern[pi + 2]:
                            hit = True
                        pi += 3
                    else:
                        if value[vi] == pattern[pi]:
                            hit = True
                        pi += 1
                if pi < len(pattern):
                    pi += 1  # consume ']'
                if hit == negate:
                    return False
                vi += 1
                continue
            if char == "\\" and pi + 1 < len(pattern):
                pi += 1
                char = pattern[pi]
            if value[vi] != char:
                return False
            pi, vi = pi + 1, vi + 1
        return vi == len(value)

    return match(0, 0)


class FakePipeline:
    """Buffers commands the way redis-py's pipeline does — every queued call returns
    the pipeline, and ``execute`` replays them against the fake in order."""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Callable[..., FakePipeline]:
        def queue(*args: Any, **kwargs: Any) -> FakePipeline:
            self._queued.append((name, args, kwargs))
            return self

        return queue

    async def execute(self) -> list[Any]:
        results = [await getattr(self._redis, name)(*args, **kwargs) for name, args, kwargs in self._queued]
        self._queued.clear()
        return results


class FakeRedis:
    """In-memory stand-in for the async redis commands the session, transcript,
    question, and entry-gate stores use: strings (set nx/ex, get, getex, getdel,
    delete, incr), key TTLs (expire), a glob-honoring ``scan_iter``, streams (xadd
    with MAXLEN, xrange, xrevrange, xread), and pipelines. Responses are decoded
    strings, matching the ``decode_responses=True`` the store connects with."""

    def __init__(self, events: list[tuple[str, str]] | None = None) -> None:
        self.store: dict[str, str] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.ttls: dict[str, int] = {}
        self._seq: dict[str, int] = {}
        self.events = events if events is not None else []

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        self.events.append(("redis_set", key))
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def getex(self, key: str, ex: int | None = None) -> str | None:
        self.events.append(("redis_getex", key))
        value = self.store.get(key)
        if value is not None and ex is not None:
            self.ttls[key] = ex
        return value

    async def getdel(self, key: str) -> str | None:
        self.events.append(("redis_getdel", key))
        self.ttls.pop(key, None)
        return self.store.pop(key, None)

    async def delete(self, key: str) -> int:
        self.ttls.pop(key, None)
        return 0 if self.store.pop(key, None) is None else 1

    async def incr(self, key: str) -> int:
        self.events.append(("redis_incr", key))
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    async def scan_iter(self, match: str | None = None) -> AsyncIterator[str]:
        # A snapshot of the keyspace, glob-filtered on ``match`` exactly as Redis
        # SCAN does — the escape test depends on ``\\*`` matching a literal ``*``.
        for key in list(self.store.keys()):
            if match is None or _redis_glob_match(match, key):
                yield key

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def xadd(self, key: str, fields: dict[str, str], maxlen: int | None = None, approximate: bool = True) -> str:
        self.events.append(("redis_xadd", key))
        stream = self.streams.setdefault(key, [])
        # Monotonic per-key ids that survive MAXLEN trimming, so a captured cursor
        # never repeats or regresses (mirrors real millisecond-seq stream ids).
        seq = self._seq[key] = self._seq.get(key, 0) + 1
        entry_id = f"{seq}-0"
        stream.append((entry_id, {str(k): str(v) for k, v in fields.items()}))
        # ``MAXLEN ~`` trims only whole macro-nodes on a real server, so the stream
        # stays over the cap for a while; the fake keeps everything, which is what
        # makes an approximate cap tell itself apart from an exact one.
        if maxlen is not None and not approximate and len(stream) > maxlen:
            del stream[: len(stream) - maxlen]
        return entry_id

    async def expire(self, key: str, ttl: int) -> bool:
        if key not in self.store and key not in self.streams:
            return False
        self.ttls[key] = ttl
        return True

    async def xrange(
        self, key: str, min: str = "-", max: str = "+", count: int | None = None
    ) -> list[tuple[str, dict[str, str]]]:
        entries = [entry for entry in self.streams.get(key, []) if _in_range(entry[0], min, max)]
        return entries[:count] if count is not None else entries

    async def xrevrange(self, key: str, count: int | None = None) -> list[tuple[str, dict[str, str]]]:
        entries = list(reversed(self.streams.get(key, [])))
        return entries[:count] if count is not None else entries

    async def xread(self, streams: dict[str, str], block: int | None = None) -> list[tuple[str, list]]:
        result: list[tuple[str, list]] = []
        for key, cursor in streams.items():
            entries = [(eid, fields) for eid, fields in self.streams.get(key, []) if _entry_gt(eid, cursor)]
            if entries:
                result.append((key, entries))
        return result


@pytest.fixture
def stub_app() -> Iterator[_StubApp]:
    yield _stub_app
    _stub_app.clients.by_class.clear()
    _stub_app.clients.ctx_kwargs.clear()
    _stub_app.conversations = _StubConversations()


_ENV_VARS = (
    "CHANNEL_WEB_REDIS_URL",
    "CHANNEL_WEB_HTTP_TIMEOUT_SECONDS",
    "CHANNEL_WEB_TRANSCRIPT_MAX_ENTRIES",
    "CHANNEL_WEB_TRANSCRIPT_TTL_SECONDS",
    "CHANNEL_WEB_BACKLOG_BATCH_ENTRIES",
    "CHANNEL_WEB_KEEPALIVE_SECONDS",
    "CHANNEL_WEB_BLOCKING_GRACE_SECONDS",
    "CHANNEL_WEB_SESSION_COOKIE_SECURE",
    "CHANNEL_WEB_SESSION_TTL_SECONDS",
    "CHANNEL_WEB_SESSION_PENDING_TTL_SECONDS",
    "CHANNEL_WEB_MAX_ANSWER_RESTORES",
    "CHANNEL_WEB_MAX_BODY_BYTES",
    "CHANNEL_WEB_MAX_STREAMS_PER_VISITOR",
    "CHANNEL_WEB_MAX_STREAMS_TOTAL",
    "CHANNEL_WEB_PAGE_TITLE",
)

# Shared test fixtures. SESSION_TOKEN is shaped like a minted cookie token (urlsafe
# alphabet, >=22 chars) so the doors' cookie shape check accepts it; VISITOR_ID is
# the non-secret address it is registered against — the transcript key's second half
# and the conversation ``client_address``. IDENTITY is the web route the session is
# minted on, OTHER_IDENTITY a second route the same visitor's cookie must not serve.
IDENTITY = "site-alpha"
OTHER_IDENTITY = "site-beta"
SESSION_TOKEN = "MZ3n-visitor_session-token-0123456789"
VISITOR_ID = "vis-0123456789ab"
CALLBACK = "https://app.example/api/interactions/callback/ticket-1"
# The host the hand-rolled requests below carry, and the origin it makes them reach.
HOST = "chat.example"
ORIGIN = f"https://{HOST}"
# The direct peer of a hand-rolled request. No door reads it — the ASGI scope simply
# always carries one.
CLIENT_HOST = "203.0.113.7"
# The two cookie names, one per deployment mode. The tests run on the default (Secure)
# settings, so that is the name a hand-rolled request sends unless it says otherwise.
SECURE_COOKIE = session_cookie_name(True)
PLAIN_COOKIE = session_cookie_name(False)
# The fixture build's file names — content-hashed exactly like a real build's.
ENTRY_ASSET = "tai42-channel-web-A1b2C3d4.js"
STYLE_ASSET = "tai42-channel-web-E5f6G7h8.css"


@pytest.fixture
def web_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CHANNEL_WEB_REDIS_URL", "redis://test/0")
    # The cached settings singletons must never leak a previous test's env.
    reset_all_settings()
    yield
    reset_all_settings()


@pytest.fixture
def no_web_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ``CHANNEL_WEB_*`` env at all, settings caches reset before and after — the
    unconfigured/fail-closed branches."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # The transcript store also falls back to the shared default namespace.
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    reset_all_settings()
    try:
        yield
    finally:
        reset_all_settings()


@pytest.fixture
def fake_httpx(stub_app: _StubApp) -> FakeHttpx:
    fake = FakeHttpx()
    stub_app.clients.by_class[HttpxClient] = fake
    return fake


@pytest.fixture
def fake_redis(stub_app: _StubApp, fake_httpx: FakeHttpx) -> FakeRedis:
    # Share one event log with the HTTP fake so ordering across the two backends
    # (reserve-before-append, claim-before-forward) is assertable.
    fake = FakeRedis(events=fake_httpx.events)
    stub_app.clients.by_class[RedisClient] = fake
    return fake


@pytest.fixture
def registered_session(fake_redis: FakeRedis) -> FakeRedis:
    """``SESSION_TOKEN`` registered against ``VISITOR_ID`` on ``IDENTITY``, as the chat
    page for that route would have left it — the state every authenticated door test
    starts from."""
    register(fake_redis, SESSION_TOKEN, VISITOR_ID, IDENTITY)
    return fake_redis


def register(fake: FakeRedis, token: str, visitor_id: str, identity: str, params: dict[str, str] | None = None) -> None:
    """Seed one session registration exactly as ``store.register_session`` writes it:
    the address the token resolves to, the web route it was minted on, and its captured
    link params. The ``params`` key is ALWAYS present (empty dict included) — the strict
    decode refuses any other shape, so an old-shape record is seeded by hand, not here."""
    fake.store[f"channel:web:session:{token}"] = json.dumps(
        {
            "visitor_id": visitor_id,
            "identity": identity,
            "created_at": datetime.now(UTC).isoformat(),
            "params": params or {},
        }
    )


def pytest_collection_finish(session: pytest.Session) -> None:
    """Hand the shared handle back to its pre-import impl once collection is done.

    The module-level ``tai42_app.bind(_stub_app)`` must stand through collection so
    test_routes's import-time route registration lands on the stub. It is undone here —
    before the run phase — but ONLY when the stub is still the active binding: a
    cross-package run whose other package bound the real app AFTER this conftest imported
    (its conftest collected later) leaves that binding untouched, so its tests, which may
    run first, see the real app rather than the leaked stub. Each of this package's own
    tests rebinds the stub through ``_bound_stub_app``."""
    if cast("Any", tai42_app)._impl is _stub_app:
        tai42_app.bind(_pre_import_impl)


@pytest.fixture(autouse=True)
def _bound_stub_app() -> Iterator[None]:
    """Bind the stub app for the duration of each test through the handle's own restoring
    context manager, so the test runs against the stub and whatever was bound going in is
    handed back on teardown. Collection-time binding and its pre-run restore are
    ``pytest_collection_finish``'s job."""
    with tai42_app.bound(_stub_app):
        yield


@pytest.fixture(autouse=True)
def _fresh_build_cache() -> Iterator[None]:
    """``load_build`` caches the parsed manifest for the process; a test that
    repoints the bundle seam must not inherit (or leave) another's build."""
    page.load_build.cache_clear()
    yield
    page.load_build.cache_clear()


@pytest.fixture(autouse=True)
def _no_leaked_stream_slots() -> Iterator[None]:
    """Every SSE stream slot must be given back by the end of a test — a leak silently
    shrinks the next test's (and a real process's) ceiling.

    A test that only opens the door and never drives the body counts too: the slot is
    taken inside the generator precisely because a generator the caller never advances
    runs no ``finally``, so a door that took one itself would leak here."""
    stream._open_streams.clear()
    yield
    assert stream._open_streams == {}, f"stream slots leaked: {stream._open_streams}"


@pytest.fixture
def public_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A fixture chat-page build: a manifest plus the files it lists, with the page
    module's bundle-directory seam pointed at it."""
    bundle = tmp_path / "public"
    bundle.mkdir()
    (bundle / ENTRY_ASSET).write_text("export const app = 1;\n", encoding="utf-8")
    (bundle / STYLE_ASSET).write_text(":root{color:inherit}\n", encoding="utf-8")
    write_manifest(bundle)
    monkeypatch.setattr(page, "_public_dir", lambda: bundle)
    return bundle


def write_manifest(bundle: Path, **overrides: Any) -> Path:
    """Write ``public-manifest.json`` into ``bundle``; ``overrides`` replace whole
    top-level keys (``None`` drops one) so a test can build a malformed manifest."""
    manifest: dict[str, Any] = {
        "name": "tai42_channel_web",
        "version": "0.1.0",
        "entry": ENTRY_ASSET,
        "styles": [STYLE_ASSET],
        "integrity": {ENTRY_ASSET: f"sha384-{ENTRY_ASSET}", STYLE_ASSET: f"sha384-{STYLE_ASSET}"},
    }
    for key, value in overrides.items():
        if value is None:
            manifest.pop(key, None)
        else:
            manifest[key] = value
    path = bundle / page.PUBLIC_MANIFEST_FILENAME
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def make_delivery(**overrides: Any) -> ChannelDelivery:
    """A valid ``ChannelDelivery`` addressed to ``IDENTITY:VISITOR_ID`` with a
    tz-aware deadline a few minutes ahead."""
    fields: dict[str, Any] = {
        "interaction_id": "int-1",
        "recipient": f"{IDENTITY}:{VISITOR_ID}",
        "question": "Deploy to prod?",
        "answer_format": "text",
        "options": None,
        "callback_url": CALLBACK,
        "timeout_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    fields.update(overrides)
    return ChannelDelivery(**fields)


def make_notification(**overrides: Any) -> ChannelNotification:
    """A valid text ``ChannelNotification`` from ``IDENTITY`` to ``VISITOR_ID``."""
    fields: dict[str, Any] = {
        "message": "Working on it.",
        "recipient": VISITOR_ID,
        "sender_identity": IDENTITY,
    }
    fields.update(overrides)
    return ChannelNotification(**fields)


def build_request(
    *,
    method: str = "POST",
    path: str = "/api/channels/web/messages",
    json_body: Any = None,
    raw_body: bytes | None = None,
    query: str = "",
    path_params: dict[str, str] | None = None,
    token: str | None = None,
    cookie_name: str = SECURE_COOKIE,
    cookie: str | None = None,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    """A Starlette ``Request`` over a minimal ASGI scope with a single-message body.

    ``token`` sends the visitor session cookie under ``cookie_name`` (the Secure
    deployment's name by default); ``cookie`` sends a raw Cookie header instead (a
    malformed value); ``extra_headers`` adds anything else (an ``Origin``, a
    forwarding header)."""
    if raw_body is not None:
        body = raw_body
    elif json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
    else:
        body = b""
    headers = [(b"content-type", b"application/json"), (b"host", HOST.encode("ascii"))]
    if token is not None:
        headers.append((b"cookie", f"{cookie_name}={token}".encode("ascii")))
    elif cookie is not None:
        headers.append((b"cookie", cookie.encode("ascii")))
    headers += extra_headers or []
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("ascii"),
        "scheme": "https",
        "server": ("app-internal", 8000),
        "client": (CLIENT_HOST, 4711),
        "headers": headers,
        "path_params": path_params or {},
    }
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(scope, receive)


def response(status_code: int, *, json: Any = None, text: str | None = None) -> httpx.Response:
    """A real ``httpx.Response`` (with a request attached) for the fake client queues."""
    kwargs: dict[str, Any] = {"request": httpx.Request("POST", "https://queued.example/")}
    if json is not None:
        kwargs["json"] = json
    if text is not None:
        kwargs["text"] = text
    return httpx.Response(status_code, **kwargs)
