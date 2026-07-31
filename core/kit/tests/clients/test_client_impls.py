"""Concrete pooled-client impls: construction, the serializable pool key, close,
and the per-impl disconnection set. The network driver is always mocked — no
real Redis/Postgres/curl/MCP connection is opened.

Impls whose extra is not installed (redis/curl/postgres) importorskip, matching
the conformance-test pattern.
"""

import asyncio
import importlib
from typing import TYPE_CHECKING, cast

import httpx
import pytest
from pydantic import SecretStr

# httpx/fastmcp ride on base deps and import unconditionally; redis/curl/postgres
# are loaded lazily in their sections via importorskip.
from tai42_kit.clients.impl import http as http_mod
from tai42_kit.clients.impl import mcp as mcp_mod
from tai42_kit.clients.settings import PostgresConnectionSettings, RedisConnectionSettings
from tai42_kit.settings.cache_registry import reset_all_settings

if TYPE_CHECKING:
    # Type-only: the runtime handle comes from the importorskip helper below, and
    # a module-level variable is not a valid type expression.
    from redis.asyncio import Redis as AsyncRedis


# ---------------------------------------------------------------------------
# httpx client (core dep — always runs)
# ---------------------------------------------------------------------------
async def test_httpx_create_applies_limits_and_timeout(monkeypatch):
    captured = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(http_mod.httpx, "AsyncClient", _FakeClient)
    await http_mod.HttpxClient()._create(
        timeout=12.0, max_connections=5, max_keepalive_connections=2, keepalive_expiry=30.0
    )
    # Ambient proxy env is ignored, and the limit kwargs flow into httpx.Limits.
    assert captured["trust_env"] is False
    assert captured["limits"].max_connections == 5
    assert captured["limits"].max_keepalive_connections == 2
    # timeout present -> wrapped in an httpx.Timeout.
    assert isinstance(captured["timeout"], http_mod.httpx.Timeout)


async def test_httpx_create_defaults_without_optional_kwargs(monkeypatch):
    captured = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(http_mod.httpx, "AsyncClient", _FakeClient)
    await http_mod.HttpxClient()._create()
    assert captured["trust_env"] is False
    # No timeout kwarg -> httpx default kept (key absent).
    assert "timeout" not in captured


async def test_httpx_create_rejects_unknown_kwarg():
    # An unknown connection kwarg is a typo that would split the pool key into a
    # separate mis-configured client — reject it loudly instead.
    with pytest.raises(ValueError, match="unknown connection kwarg"):
        await http_mod.HttpxClient()._create(timeout=1.0, bogus=1)


async def test_httpx_close_calls_aclose(monkeypatch):
    closed = []

    class _FakeClient:
        async def aclose(self):
            closed.append(True)

    # The fake only implements aclose(), the single method _close() invokes.
    await http_mod.HttpxClient()._close(cast(httpx.AsyncClient, _FakeClient()))
    assert closed == [True]


# ---------------------------------------------------------------------------
# Redis client (extra `redis` — importorskip)
# ---------------------------------------------------------------------------
def _redis_mod():
    pytest.importorskip("redis")
    return importlib.import_module("tai42_kit.clients.impl.redis")


def test_redis_validate_url_errors():
    redis_mod = _redis_mod()
    with pytest.raises(KeyError):
        redis_mod._validate_url({})
    with pytest.raises(ValueError, match="cannot be empty"):
        redis_mod._validate_url({"url": ""})
    with pytest.raises(TypeError):
        redis_mod._validate_url({"url": 123})


async def test_redis_create_rejects_unknown_kwarg():
    redis_mod = _redis_mod()
    with pytest.raises(ValueError, match="unknown connection kwarg"):
        await redis_mod.RedisClient()._create(url="redis://localhost:6379/0", bogus=1)


def test_redis_with_retry_zero_leaves_kwargs_unchanged():
    redis_mod = _redis_mod()
    out = redis_mod._with_retry({"url": "redis://x", "retry_attempts": 0})
    assert "retry" not in out
    assert "retry_on_error" not in out


def test_redis_with_retry_builds_retry_async():
    redis_mod = _redis_mod()
    from redis.asyncio.retry import Retry

    out = redis_mod._with_retry({"url": "redis://x", "retry_attempts": 3})
    assert isinstance(out["retry"], Retry)
    assert out["retry_on_error"]  # connection/timeout errors registered


def test_redis_with_retry_builds_retry_sync():
    redis_mod = _redis_mod()
    from redis.retry import Retry

    out = redis_mod._with_retry({"url": "redis://x", "retry_attempts": 2}, sync=True)
    assert isinstance(out["retry"], Retry)


async def test_redis_async_create_uses_from_url(monkeypatch):
    redis_mod = _redis_mod()
    captured = {}

    class _FakeAsyncRedis:
        async def initialize(self):
            return self

    def _from_url(**kwargs):
        captured.update(kwargs)
        return _FakeAsyncRedis()

    monkeypatch.setattr(redis_mod.AsyncRedis, "from_url", staticmethod(_from_url))
    client = await redis_mod.RedisClient()._create(url="redis://localhost:6379/0", retry_attempts=0)
    assert isinstance(client, _FakeAsyncRedis)
    assert captured["url"] == "redis://localhost:6379/0"


async def test_redis_create_accepts_socket_connect_timeout(monkeypatch):
    # The kwarg is in _ALLOWED_KWARGS, so it is not rejected and flows through to
    # redis.from_url (which forwards it to the connection natively).
    redis_mod = _redis_mod()
    assert "socket_connect_timeout" in redis_mod._ALLOWED_KWARGS
    captured = {}

    class _FakeAsyncRedis:
        async def initialize(self):
            return self

    def _from_url(**kwargs):
        captured.update(kwargs)
        return _FakeAsyncRedis()

    monkeypatch.setattr(redis_mod.AsyncRedis, "from_url", staticmethod(_from_url))
    await redis_mod.RedisClient()._create(url="redis://localhost:6379/0", socket_connect_timeout=2.5)
    assert captured["socket_connect_timeout"] == 2.5


async def test_redis_async_close_calls_aclose():
    redis_mod = _redis_mod()
    closed = []

    class _C:
        async def aclose(self):
            closed.append(True)

    # The fake only implements aclose(), the single method _close() invokes.
    await redis_mod.RedisClient()._close(cast("AsyncRedis", _C()))
    assert closed == [True]


async def test_redis_sync_create_and_close(monkeypatch):
    redis_mod = _redis_mod()

    class _FakeSyncRedis:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(redis_mod.SyncRedis, "from_url", staticmethod(lambda **kw: _FakeSyncRedis()))
    client = await redis_mod.SyncRedisClient()._create(url="redis://localhost:6379/0")
    await redis_mod.SyncRedisClient()._close(client)
    # from_url is patched to yield the fake, so the returned client is one.
    assert cast(_FakeSyncRedis, client).closed is True


def test_redis_disconnection_exceptions():
    redis_mod = _redis_mod()
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    assert set(redis_mod.RedisClient()._disconnection_exceptions()) == {RedisConnectionError, RedisTimeoutError}
    assert set(redis_mod.SyncRedisClient()._disconnection_exceptions()) == {RedisConnectionError, RedisTimeoutError}


def test_redis_pool_key_from_settings_is_serializable():
    s = RedisConnectionSettings(
        redis_url="redis://h:6379/0", retry_attempts=2, socket_timeout=5.0, retry_on_timeout=True
    )
    kw = s.client_kwargs()
    # retry_attempts travels as a plain int (a Retry object would break the key).
    assert kw["retry_attempts"] == 2
    assert kw["socket_timeout"] == 5.0
    assert kw["retry_on_timeout"] is True
    import json

    json.dumps(kw)  # must be JSON-serializable for the pool key


# ---------------------------------------------------------------------------
# FastMCP client (fastmcp is a core dep — always runs)
# ---------------------------------------------------------------------------
async def test_mcp_create_requires_config():
    with pytest.raises(KeyError, match="config"):
        await mcp_mod.FastMCPClient()._create()


async def test_mcp_create_rejects_non_dict_config():
    with pytest.raises(TypeError, match="must be a non-empty dict"):
        await mcp_mod.FastMCPClient()._create(config=None)
    with pytest.raises(TypeError, match="must be a non-empty dict"):
        await mcp_mod.FastMCPClient()._create(config="not-a-dict")


async def test_mcp_create_rejects_unknown_kwarg():
    with pytest.raises(ValueError, match="unknown connection kwarg"):
        await mcp_mod.FastMCPClient()._create(config={"url": "http://h/mcp"}, bogus=1)


async def test_mcp_create_builds_client_from_transport(monkeypatch):
    captured = {}

    class _FakeClient:
        def __init__(self, transport):
            captured["transport"] = transport

        async def __aenter__(self):
            captured["entered"] = True
            return self

        async def __aexit__(self, *a):
            captured["exited"] = True

    monkeypatch.setattr(mcp_mod, "Client", _FakeClient)
    monkeypatch.setattr(mcp_mod, "get_mcp_transport", lambda cfg, uds_protocol: ("T", uds_protocol))

    client = await mcp_mod.FastMCPClient()._create(
        config={"title": "srv", "config": {"url": "http://h/mcp"}}, uds_protocol="sse"
    )
    assert captured["entered"] is True
    assert captured["transport"] == ("T", "sse")
    await mcp_mod.FastMCPClient()._close(client)
    assert captured["exited"] is True


async def test_mcp_create_connect_timeout_raises_named(monkeypatch):
    # A connect/init that never completes is bounded by
    # MCP_CLIENT_CONNECT_TIMEOUT_SECONDS; the raised TimeoutError names the env var.
    class _BlockingClient:
        def __init__(self, transport):
            pass

        async def __aenter__(self):
            await asyncio.Event().wait()  # never set -> blocks forever

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(mcp_mod, "Client", _BlockingClient)
    monkeypatch.setattr(mcp_mod, "get_mcp_transport", lambda cfg, uds_protocol: "T")
    monkeypatch.setenv("MCP_CLIENT_CONNECT_TIMEOUT_SECONDS", "0.01")
    reset_all_settings()
    try:
        with pytest.raises(TimeoutError, match="MCP_CLIENT_CONNECT_TIMEOUT_SECONDS"):
            await mcp_mod.FastMCPClient()._create(config={"title": "srv", "config": {"url": "http://h/mcp"}})
    finally:
        reset_all_settings()


def test_mcp_disconnection_exceptions():
    import anyio

    exc = mcp_mod.FastMCPClient()._disconnection_exceptions()
    assert anyio.ClosedResourceError in exc
    assert anyio.BrokenResourceError in exc
    # RuntimeError is deliberately NOT in the blanket set: it is matched by
    # message via _is_disconnection_error, never wholesale.
    assert RuntimeError not in exc


def test_mcp_runtime_error_matched_by_message_only():
    import anyio

    client = mcp_mod.FastMCPClient()
    # Loop-bound RuntimeErrors from anyio/asyncio count as disconnections.
    assert client._is_disconnection_error(RuntimeError("Event loop is closed")) is True
    assert client._is_disconnection_error(RuntimeError("got Future attached to a different loop")) is True
    # An arbitrary RuntimeError from caller code does not tear down the pool.
    assert client._is_disconnection_error(RuntimeError("caller bug")) is False
    # The declared set still matches via the base predicate.
    assert client._is_disconnection_error(anyio.ClosedResourceError()) is True


# ---------------------------------------------------------------------------
# curl client (extra `curl` — importorskip)
# ---------------------------------------------------------------------------
def _curl_mod():
    pytest.importorskip("curl_cffi")
    return importlib.import_module("tai42_kit.clients.impl.curl")


async def test_curl_create_strips_session_key_and_builds_session(monkeypatch):
    curl_mod = _curl_mod()
    captured = {}

    class _FakeSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.closed = False

        async def close(self):
            self.closed = True

    monkeypatch.setattr(curl_mod.requests, "AsyncSession", _FakeSession)
    caller_params = {"session_params": {"timeout": 10, "session_key": "drop-me"}}
    session = await curl_mod.CurlClient()._create(**caller_params)
    # session_key is stripped; the caller's dict is never mutated.
    assert captured == {"timeout": 10}
    assert caller_params["session_params"] == {"timeout": 10, "session_key": "drop-me"}
    await curl_mod.CurlClient()._close(session)
    assert session.closed is True


async def test_curl_create_rejects_unknown_kwarg():
    # An unknown connection kwarg is a typo that would split the pool key into a
    # separate mis-configured session — reject it loudly instead.
    curl_mod = _curl_mod()
    with pytest.raises(ValueError, match="unknown connection kwarg"):
        await curl_mod.CurlClient()._create(session_params={"timeout": 10}, bogus=1)


async def test_curl_close_noop_on_falsy():
    curl_mod = _curl_mod()
    # A falsy client short-circuits (no .close call) without error.
    await curl_mod.CurlClient()._close(None)


def test_curl_disconnection_exceptions():
    curl_mod = _curl_mod()
    import anyio
    from curl_cffi.requests.exceptions import SessionClosed

    exc = curl_mod.CurlClient()._disconnection_exceptions()
    assert anyio.ClosedResourceError in exc
    assert SessionClosed in exc
    # RuntimeError is deliberately NOT in the blanket set: it is matched by
    # message via _is_disconnection_error, never wholesale.
    assert RuntimeError not in exc


def test_curl_runtime_error_matched_by_message_only():
    curl_mod = _curl_mod()
    from curl_cffi.requests.exceptions import SessionClosed

    client = curl_mod.CurlClient()
    # Loop-bound RuntimeErrors from curl_cffi/anyio count as disconnections.
    assert client._is_disconnection_error(RuntimeError("Event loop is closed")) is True
    assert client._is_disconnection_error(RuntimeError("got Future attached to a different loop")) is True
    # An arbitrary RuntimeError from caller code does not tear down the pool.
    assert client._is_disconnection_error(RuntimeError("caller bug")) is False
    # The declared set still matches via the base predicate.
    assert client._is_disconnection_error(SessionClosed("closed")) is True


# ---------------------------------------------------------------------------
# Postgres client (extra `postgres` — importorskip)
# ---------------------------------------------------------------------------
def _pg_mod():
    pytest.importorskip("psycopg_pool")
    return importlib.import_module("tai42_kit.clients.impl.postgres")


async def test_pg_create_requires_dsn():
    pg_mod = _pg_mod()
    with pytest.raises(KeyError, match="dsn"):
        await pg_mod.PostgresClient()._create()


async def test_pg_create_rejects_empty_or_nonstring_dsn():
    pg_mod = _pg_mod()
    with pytest.raises(ValueError, match="non-empty string"):
        await pg_mod.PostgresClient()._create(dsn="")
    with pytest.raises(ValueError, match="non-empty string"):
        await pg_mod.PostgresClient()._create(dsn=123)


async def test_pg_create_rejects_unknown_kwarg():
    pg_mod = _pg_mod()
    with pytest.raises(ValueError, match="unknown connection kwarg"):
        await pg_mod.PostgresClient()._create(dsn="postgresql://u@h/db", bogus=1)


async def test_pg_create_opens_pool_and_close(monkeypatch):
    pg_mod = _pg_mod()
    captured = {}

    class _FakePool:
        @staticmethod
        async def check_connection(conn):
            pass

        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.opened = False
            self.closed = False

        async def open(self):
            self.opened = True

        async def close(self):
            self.closed = True

    monkeypatch.setattr(pg_mod, "AsyncConnectionPool", _FakePool)
    pool = await pg_mod.PostgresClient()._create(dsn="postgresql://u@h/db", min_size=3, max_size=7)
    assert captured["conninfo"] == "postgresql://u@h/db"
    assert captured["min_size"] == 3
    assert captured["max_size"] == 7
    assert captured["open"] is False  # opened explicitly after construction
    assert captured["check"] is _FakePool.check_connection  # liveness check on checkout
    assert pool.opened is True
    await pg_mod.PostgresClient()._close(pool)
    assert pool.closed is True


def test_pg_disconnection_exceptions():
    pg_mod = _pg_mod()
    import psycopg

    assert pg_mod.PostgresClient()._disconnection_exceptions() == (psycopg.OperationalError,)


def test_pg_pool_key_from_settings():
    s = PostgresConnectionSettings(pg_host="h", pg_port=5433, pg_db="d", pg_user="u", pg_password=SecretStr("p"))
    kw = s.client_kwargs()
    assert kw == {
        "dsn": "postgresql://u:p@h:5433/d?connect_timeout=10&options=-c%20statement_timeout%3D60000",
        "min_size": 2,
        "max_size": 10,
    }
