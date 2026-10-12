"""Connector auth glue: resolve, inject, error framing, force-refresh retry."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast

import httpx
import mcp.types
import pytest
from tai42_contract.connectors.models import ConnectorRef
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.errors import ClientConnectError, ErrorKind, error_kind
from tai42_contract.manifest import MCPConfig, TaiMCPConfig
from tai42_kit.clients.impl.mcp import FastMCPClient

import tai42_skeleton.connectors.token_injection as ti
from tai42_skeleton.connectors.providers import registry
from tai42_skeleton.connectors.runtime.resolver import (
    ConnectorAuthExpiredError,
    ConnectorConnectionError,
    ConnectorInsufficientScopeError,
    ManagedAuth,
)
from tai42_skeleton.connectors.token_injection import (
    CONNECTOR_ERROR_PREFIX,
    CONNECTOR_META_TOKEN_KEY,
    check_managed_transport,
    extract_connector_error_payload,
    is_token_expired,
    resolve_managed_auth_for_config,
)

from . import mcp_fakes
from .conftest import CID, make_noauth_http_descriptor, make_noauth_stdio_descriptor, make_oauth_descriptor


def _managed_http(url="https://acme.test/mcp/mail", headers=None):
    return TaiMCPConfig(
        title="acme_mail_work",
        config=MCPConfig(type="http", url=url, headers=headers or {}),
        managed=ConnectorRef(connection_id=CID, provider_id="acme", sub_service="mail"),
    )


def _managed_stdio(env=None):
    return TaiMCPConfig(
        title="widgets_search_main",
        config=MCPConfig(type="stdio", command="uvx", args=["x"], env=env or {}),
        managed=ConnectorRef(connection_id=CID, provider_id="widgets", sub_service="search"),
    )


def _unmanaged():
    return TaiMCPConfig(title="hand", config=MCPConfig(type="http", url="https://x/mcp"))


def _result(*, is_error, texts) -> mcp.types.CallToolResult:
    # extract_connector_error_payload reads only .isError, .content and each
    # block's .text. CallToolResult is a concrete pydantic model whose content
    # requires typed ContentBlocks, so the lightweight SimpleNamespace fake is
    # cast to it.
    return cast(
        mcp.types.CallToolResult,
        SimpleNamespace(
            isError=is_error,
            content=[SimpleNamespace(text=t) for t in texts],
        ),
    )


# -- resolve_managed_auth_for_config -----------------------------------------


async def test_resolve_unmanaged_returns_none(monkeypatch):
    assert await resolve_managed_auth_for_config(_unmanaged()) is None


async def test_resolve_oauth_returns_auth(monkeypatch, acme):
    async def fake_resolve(cid, pid, ss, *, allow_refresh=True):
        return ManagedAuth(access_token="tok")

    monkeypatch.setattr(ti, "resolve_managed_auth", fake_resolve)
    auth = await resolve_managed_auth_for_config(_managed_http())
    assert auth is not None
    assert auth.access_token == "tok"


async def test_resolve_oauth_empty_token_rejected(monkeypatch, acme):
    async def fake_resolve(cid, pid, ss, *, allow_refresh=True):
        return ManagedAuth(access_token="")

    monkeypatch.setattr(ti, "resolve_managed_auth", fake_resolve)
    # An empty-but-present access_token is never sent as if authenticated.
    with pytest.raises(RuntimeError, match="empty access_token"):
        await resolve_managed_auth_for_config(_managed_http())


async def test_resolve_no_auth_returns_auth(monkeypatch):
    async def fake_resolve(cid, pid, ss, *, allow_refresh=True):
        return ManagedAuth(env={"api_key": "k"})

    monkeypatch.setattr(ti, "resolve_managed_auth", fake_resolve)
    auth = await resolve_managed_auth_for_config(_managed_stdio())
    assert auth is not None
    assert auth.env == {"api_key": "k"}


async def test_resolve_returns_none_when_resolver_none(monkeypatch):
    async def fake_resolve(cid, pid, ss, *, allow_refresh=True):
        return None

    monkeypatch.setattr(ti, "resolve_managed_auth", fake_resolve)
    assert await resolve_managed_auth_for_config(_managed_stdio()) is None


# -- token injection / prepare_managed_request --------------------------------------


def test_prepare_request_no_auth_injects_nothing():
    config = _managed_http()
    out_config, meta = ti.prepare_managed_request(config, None, "http")
    assert out_config is config
    assert meta is None


def test_prepare_request_oauth_stdio_sets_meta():
    config = _managed_stdio()
    _out_config, meta = ti.prepare_managed_request(config, ManagedAuth(access_token="tok"), "stdio")
    assert meta == {CONNECTOR_META_TOKEN_KEY: "tok"}


def test_prepare_request_oauth_http_sets_bearer():
    config = _managed_http()
    out_config, meta = ti.prepare_managed_request(config, ManagedAuth(access_token="tok"), "http")
    assert meta is None
    assert out_config.config.headers is not None
    assert out_config.config.headers["authorization"] == "Bearer tok"


def test_prepare_request_no_auth_stdio_merges_env():
    config = _managed_stdio(env={"BASE": "1"})
    out_config, meta = ti.prepare_managed_request(config, ManagedAuth(env={"api_key": "k"}), "stdio")
    assert out_config.config.env == {"BASE": "1", "api_key": "k"}
    assert meta is None


def test_prepare_request_no_auth_http_merges_headers():
    config = _managed_http(headers={"X-Base": "1"})
    out_config, _meta = ti.prepare_managed_request(config, ManagedAuth(headers={"X-Tok": "v"}), "http")
    assert out_config.config.headers == {"x-base": "1", "x-tok": "v"}


def test_merge_http_auth_lowercases_existing_headers():
    config = _managed_http(headers={"Authorization": "old", "X-Keep": "1"})
    merged = ti._merge_http_auth(config, ManagedAuth(access_token="new"))
    assert merged.config.headers is not None
    assert merged.config.headers["authorization"] == "Bearer new"
    assert merged.config.headers["x-keep"] == "1"


# -- check_managed_transport -------------------------------------------------


def test_check_managed_transport_allows_http():
    check_managed_transport(_managed_http(), "http")  # no raise


def test_check_managed_transport_rejects_unsupported():
    with pytest.raises(RuntimeError, match="not supported"):
        check_managed_transport(_managed_http(), "websocket")


def test_check_managed_transport_ignores_unmanaged():
    check_managed_transport(_unmanaged(), "websocket")  # no raise


# -- extract_connector_error_payload -----------------------------------------


def test_extract_returns_none_when_not_error():
    assert extract_connector_error_payload(_result(is_error=False, texts=["x"])) is None


def test_extract_parses_framed_payload():
    payload = json.dumps({"code": "token_expired"})
    result = _result(is_error=True, texts=[f"{CONNECTOR_ERROR_PREFIX}{payload}"])
    assert extract_connector_error_payload(result) == {"code": "token_expired"}


def test_extract_parses_after_fastmcp_framing():
    payload = json.dumps({"code": "boom"})
    text = f"Error calling tool 'mytool': {CONNECTOR_ERROR_PREFIX}{payload}"
    result = _result(is_error=True, texts=[text])
    assert extract_connector_error_payload(result) == {"code": "boom"}


def test_extract_rejects_forged_prefix_mid_string():
    payload = json.dumps({"code": "token_expired"})
    # prefix not at an anchored position → not trusted
    result = _result(is_error=True, texts=[f"user said: {CONNECTOR_ERROR_PREFIX}{payload}"])
    assert extract_connector_error_payload(result) is None


def test_extract_skips_bad_json():
    result = _result(is_error=True, texts=[f"{CONNECTOR_ERROR_PREFIX}not-json"])
    assert extract_connector_error_payload(result) is None


def test_extract_skips_payload_without_code():
    payload = json.dumps({"detail": "x"})
    result = _result(is_error=True, texts=[f"{CONNECTOR_ERROR_PREFIX}{payload}"])
    assert extract_connector_error_payload(result) is None


def test_extract_skips_non_text_blocks():
    # A non-text block (text=None) is not expressible as a real ContentBlock, so
    # the SimpleNamespace fake is cast to the concrete CallToolResult model.
    result = cast(
        mcp.types.CallToolResult,
        SimpleNamespace(isError=True, content=[SimpleNamespace(text=None)]),
    )
    assert extract_connector_error_payload(result) is None


# -- is_token_expired --------------------------------------------------------


def test_is_token_expired():
    assert is_token_expired({"code": "token_expired"}) is True
    assert is_token_expired({"code": "other"}) is False
    assert is_token_expired(None) is False


# -- the seam: shared doubles --------------------------------------------------------------------


def _http_error(status: int, challenge: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://acme.test/mcp/mail")
    headers = {"WWW-Authenticate": challenge} if challenge is not None else {}
    response = httpx.Response(status, request=request, headers=headers)
    return httpx.HTTPStatusError(f"Client error '{status}'", request=request, response=response)


class _FakeSession:
    """One pooled MCP session, keyed in :class:`_FakePool` by its connection config."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = outcomes
        self.close_calls = 0
        self.close_raises = False
        self.metas: list = []

    async def call_tool_mcp(self, tool_name, arguments, meta=None, timeout=None):
        self.metas.append((meta, timeout))
        nxt = self.outcomes.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    async def _close(self) -> None:
        self.close_calls += 1
        if self.close_raises:
            raise RuntimeError("close failed")


class _FakePool:
    """Stand-in for the kit pooled client: a per-key session map with the exact-key ``close``.

    Every request, whatever its key, takes the next outcome of one shared script; ``opened`` lists the
    config of every lease and ``closed`` every key closed.
    """

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.pool: dict[str, _FakeSession] = {}
        self.opened: list[dict] = []
        self.closed: list[dict] = []

    @staticmethod
    def _key(config) -> str:
        return json.dumps({"config": config}, sort_keys=True)

    def seed(self, config_dump, session: _FakeSession) -> None:
        self.pool[self._key(config_dump)] = session

    def current(self, *, config):
        self.opened.append(config)
        key = self._key(config)
        session = self.pool.get(key)
        if session is None:
            session = _FakeSession(self.outcomes)
            self.pool[key] = session

        @asynccontextmanager
        async def _cm():
            yield session

        return _cm()

    async def close(self, *, config) -> None:
        self.closed.append(config)
        session = self.pool.pop(self._key(config), None)
        if session is not None:
            await session._close()

    @property
    def requests(self) -> int:
        return len(self.opened)


def _effective_dump(config: TaiMCPConfig, auth: ManagedAuth | None, transport: str) -> dict:
    """The pool-key config a dispatch with ``auth`` opens."""
    return ti.prepare_managed_request(config, auth, transport)[0].model_dump()


def _ok() -> mcp.types.CallToolResult:
    return _result(is_error=False, texts=["done"])


def _marker() -> mcp.types.CallToolResult:
    return _result(is_error=True, texts=[f"{CONNECTOR_ERROR_PREFIX}{json.dumps({'code': 'token_expired'})}"])


async def _call(config: TaiMCPConfig, pool: _FakePool, transport: str = "http"):
    async def run(client, meta):
        return await client.call_tool_mcp("send", {}, meta=meta)

    return await ti.with_managed_auth(config, transport, cast(FastMCPClient, pool), run, tool_name="send")


class _Resolver:
    """Records every resolve and forced refresh the seam drives; hands out the scripted credentials."""

    def __init__(self, monkeypatch, *, initial: ManagedAuth | None, refreshed: str = "fresh") -> None:
        self.resolves: list[dict] = []
        self.refreshes: list[str | None] = []
        self.noted: list[list[str]] = []
        self._initial = initial
        self._refreshed = refreshed
        monkeypatch.setattr(ti, "resolve_managed_auth", self._resolve)
        monkeypatch.setattr(ti, "force_refresh", self._force)
        monkeypatch.setattr(ti, "note_scopes_wanted", self._note)

    async def _resolve(self, cid, pid, ss, *, allow_refresh=True):
        self.resolves.append({"connection_id": cid, "allow_refresh": allow_refresh})
        return self._initial

    async def _force(self, cid, *, failed_access_token=None):
        self.refreshes.append(failed_access_token)
        return ManagedAuth(access_token=self._refreshed)

    async def _note(self, cid, scopes):
        self.noted.append(list(scopes))
        return True


@pytest.fixture
def acme(monkeypatch) -> ProviderDescriptor:
    """The OAuth provider ``acme`` registered; its ``mail`` server is ``https://acme.test/mcp/mail``."""
    descriptor = make_oauth_descriptor()
    registry.reset_registry()
    registry.register_connector(descriptor)
    registry.register_connector(make_noauth_http_descriptor())
    registry.register_connector(make_noauth_stdio_descriptor())
    return descriptor


def _acme_entry(*, url: str = "https://acme.test/mcp/mail", title: str = "acme_mail_work", type_: str | None = "http"):
    return TaiMCPConfig(
        title=title,
        config=MCPConfig(type=type_, url=url),
        managed=ConnectorRef(connection_id=CID, provider_id="acme", sub_service="mail"),
    )


# -- call: the marker and the retry -------------------------------------------------------------


async def test_a_stdio_call_carries_the_token_in_meta_and_the_call_budget(monkeypatch):
    from tai42_skeleton.settings.mcp_settings import mcp_dispatch_settings

    _Resolver(monkeypatch, initial=ManagedAuth(access_token="tok"))
    pool = _FakePool([_ok()])
    session = _FakeSession([_ok()])
    pool.seed(_effective_dump(_managed_stdio(), ManagedAuth(access_token="tok"), "stdio"), session)
    out = await ti.call_with_auth(_managed_stdio(), "stdio", "send", {"a": 1}, cast(FastMCPClient, pool))
    assert out.isError is False
    assert session.metas == [({CONNECTOR_META_TOKEN_KEY: "tok"}, mcp_dispatch_settings().call_timeout_seconds)]


async def test_the_marker_refreshes_once_fenced_and_the_retry_answers(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="old"))
    pool = _FakePool([_marker(), _ok()])
    out = await _call(_acme_entry(), pool)
    assert out.isError is False
    assert resolver.refreshes == ["old"]
    assert pool.requests == 2


async def test_a_second_marker_raises_auth_expired(monkeypatch, acme):
    _Resolver(monkeypatch, initial=ManagedAuth(access_token="old"))
    pool = _FakePool([_marker(), _marker()])
    with pytest.raises(ConnectorAuthExpiredError):
        await _call(_acme_entry(), pool)


async def test_an_http_rotation_evicts_the_superseded_session_and_keeps_the_fresh(monkeypatch, acme):
    _Resolver(monkeypatch, initial=ManagedAuth(access_token="old"))
    config = _acme_entry()
    pool = _FakePool([_ok()])
    old_session = _FakeSession([_marker()])
    pool.seed(_effective_dump(config, ManagedAuth(access_token="old"), "http"), old_session)
    out = await _call(config, pool)
    assert out.isError is False
    assert old_session.close_calls == 1
    assert list(pool.pool) == [pool._key(_effective_dump(config, ManagedAuth(access_token="fresh"), "http"))]


async def test_a_stdio_rotation_closes_nothing(monkeypatch):
    _Resolver(monkeypatch, initial=ManagedAuth(access_token="old"))
    config = _managed_stdio()
    pool = _FakePool([])
    live = _FakeSession([_marker(), _ok()])
    pool.seed(_effective_dump(config, ManagedAuth(access_token="old"), "stdio"), live)
    out = await _call(config, pool, "stdio")
    assert out.isError is False
    assert live.close_calls == 0


async def test_a_failing_close_of_the_superseded_session_is_logged_and_not_fatal(monkeypatch, acme, caplog):
    _Resolver(monkeypatch, initial=ManagedAuth(access_token="old"))
    config = _acme_entry()
    pool = _FakePool([_ok()])
    old_session = _FakeSession([_marker()])
    old_session.close_raises = True
    pool.seed(_effective_dump(config, ManagedAuth(access_token="old"), "http"), old_session)
    with caplog.at_level(logging.WARNING):
        out = await _call(config, pool)
    assert out.isError is False
    assert "failed to close the pooled MCP session superseded by a token rotation" in caplog.text


@pytest.mark.parametrize("retry_outcome", ["error result", "second expiry"])
async def test_the_superseded_session_is_evicted_whatever_the_retry_answers(monkeypatch, acme, retry_outcome):
    _Resolver(monkeypatch, initial=ManagedAuth(access_token="old"))
    config = _acme_entry()
    second = _result(is_error=True, texts=["plain tool error"]) if retry_outcome == "error result" else _marker()
    pool = _FakePool([second])
    old_session = _FakeSession([_marker()])
    pool.seed(_effective_dump(config, ManagedAuth(access_token="old"), "http"), old_session)
    if retry_outcome == "second expiry":
        with pytest.raises(ConnectorAuthExpiredError):
            await _call(config, pool)
    else:
        await _call(config, pool)
    assert old_session.close_calls == 1


async def test_no_eviction_when_the_refresh_fails_before_the_retry(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="old"))

    async def failing_force(cid, *, failed_access_token=None):
        raise RuntimeError("refresh boom")

    monkeypatch.setattr(ti, "force_refresh", failing_force)
    config = _acme_entry()
    pool = _FakePool([])
    old_session = _FakeSession([_marker()])
    pool.seed(_effective_dump(config, ManagedAuth(access_token="old"), "http"), old_session)
    with pytest.raises(RuntimeError, match="refresh boom"):
        await _call(config, pool)
    assert old_session.close_calls == 0
    assert resolver.refreshes == []


# -- call: an HTTP 401 means the token expired -------------------------------------------------


async def test_a_401_refreshes_once_fenced_on_the_failing_token_and_the_retry_answers(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="old"))
    config = _acme_entry()
    pool = _FakePool([_ok()])
    old_session = _FakeSession([_http_error(401)])
    pool.seed(_effective_dump(config, ManagedAuth(access_token="old"), "http"), old_session)
    out = await _call(config, pool)
    assert out.isError is False
    assert resolver.refreshes == ["old"]
    assert pool.requests == 2
    assert pool.opened[1]["config"]["headers"]["authorization"] == "Bearer fresh"
    assert old_session.close_calls == 1


async def test_a_second_401_raises_auth_expired(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="old"))
    pool = _FakePool([_http_error(401), _http_error(401)])
    with pytest.raises(ConnectorAuthExpiredError) as caught:
        await _call(_acme_entry(), pool)
    assert resolver.refreshes == ["old"]
    assert pool.requests == 2
    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)


async def test_the_marker_and_a_401_share_the_retry_count(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="old"))
    pool = _FakePool([_marker(), _http_error(401)])
    with pytest.raises(ConnectorAuthExpiredError):
        await _call(_acme_entry(), pool)
    assert pool.requests == 2
    assert resolver.refreshes == ["old"]


async def test_a_401_for_a_hand_written_entry_propagates_untouched(monkeypatch):
    resolver = _Resolver(monkeypatch, initial=None)
    pool = _FakePool([_http_error(401)])
    with pytest.raises(httpx.HTTPStatusError):
        await _call(_unmanaged(), pool)
    assert resolver.refreshes == []
    assert pool.requests == 1


async def test_a_401_for_a_no_auth_entry_is_not_retried(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(headers={"token": "static"}))
    config = TaiMCPConfig(
        title="httpsvc_main_x",
        config=MCPConfig(type="http", url="https://httpsvc.test/mcp"),
        managed=ConnectorRef(connection_id=CID, provider_id="httpsvc", sub_service="main"),
    )
    pool = _FakePool([_http_error(401)])
    with pytest.raises(httpx.HTTPStatusError):
        await _call(config, pool)
    assert resolver.refreshes == []
    assert pool.requests == 1


async def test_a_plain_403_is_not_retried(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="old"))
    pool = _FakePool([_http_error(403)])
    with pytest.raises(httpx.HTTPStatusError):
        await _call(_acme_entry(), pool)
    assert resolver.refreshes == []
    assert resolver.noted == []
    assert pool.requests == 1


async def test_a_connect_time_401_takes_the_auth_path(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="old"))
    pool = _FakePool([_ok()])
    real_current = pool.current
    attempts = {"n": 0}

    def current(*, config):
        attempts["n"] += 1
        if attempts["n"] == 1:
            pool.opened.append(config)
            try:
                raise _http_error(401)
            except httpx.HTTPStatusError as cause:
                raise ClientConnectError("MCP client connect failed") from cause
        return real_current(config=config)

    monkeypatch.setattr(pool, "current", current)
    out = await _call(_acme_entry(), pool)
    assert out.isError is False
    assert resolver.refreshes == ["old"]


# -- call: a scope challenge -------------------------------------------------------------------

_SCOPE_CHALLENGE = 'Bearer error="insufficient_scope", scope="files:read files:write"'


async def test_a_scope_challenge_answers_insufficient_scope_after_one_request(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="tok"))
    config = _acme_entry()
    pool = _FakePool([])
    session = _FakeSession([_http_error(403, _SCOPE_CHALLENGE)])
    pool.seed(_effective_dump(config, ManagedAuth(access_token="tok"), "http"), session)
    with pytest.raises(ConnectorInsufficientScopeError) as caught:
        await _call(config, pool)
    assert caught.value.scopes == ["files:read", "files:write"]
    assert resolver.refreshes == []
    assert resolver.noted == [["files:read", "files:write"]]
    assert pool.requests == 1
    assert session.close_calls == 1  # the session that carried the 403 is evicted


@pytest.mark.parametrize(
    "challenge",
    [None, 'Bearer error="invalid_token"', 'Basic realm="x"'],
    ids=["no header", "another error", "another scheme"],
)
async def test_another_403_propagates_and_writes_nothing(monkeypatch, acme, challenge):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="tok"))
    pool = _FakePool([_http_error(403, challenge)])
    with pytest.raises(httpx.HTTPStatusError):
        await _call(_acme_entry(), pool)
    assert resolver.noted == []
    assert resolver.refreshes == []


async def test_a_scope_challenge_for_a_hand_written_entry_propagates(monkeypatch):
    resolver = _Resolver(monkeypatch, initial=None)
    pool = _FakePool([_http_error(403, _SCOPE_CHALLENGE)])
    with pytest.raises(httpx.HTTPStatusError):
        await _call(_unmanaged(), pool)
    assert resolver.noted == []


async def test_a_scope_challenge_for_a_no_auth_entry_propagates(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(headers={"token": "static"}))
    config = TaiMCPConfig(
        title="httpsvc_main_x",
        config=MCPConfig(type="http", url="https://httpsvc.test/mcp"),
        managed=ConnectorRef(connection_id=CID, provider_id="httpsvc", sub_service="main"),
    )
    pool = _FakePool([_http_error(403, _SCOPE_CHALLENGE)])
    with pytest.raises(httpx.HTTPStatusError):
        await _call(config, pool)
    assert resolver.noted == []


async def test_a_scope_challenge_without_scope_answers_an_empty_list_and_writes_nothing(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="tok"))
    pool = _FakePool([_http_error(403, 'Bearer error="insufficient_scope"')])
    with pytest.raises(ConnectorInsufficientScopeError) as caught:
        await _call(_acme_entry(), pool)
    assert caught.value.scopes == []
    assert resolver.noted == []


async def test_a_malformed_scope_token_is_dropped_with_a_warning(monkeypatch, acme, caplog):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="tok"))
    pool = _FakePool([_http_error(403, 'Bearer error="insufficient_scope", scope="ok:read bad\\"quote"')])
    with caplog.at_level(logging.WARNING), pytest.raises(ConnectorInsufficientScopeError) as caught:
        await _call(_acme_entry(), pool)
    assert caught.value.scopes == ["ok:read"]
    assert resolver.noted == [["ok:read"]]
    assert "outside the RFC 6749 grammar" in caplog.text


async def test_a_store_error_while_noting_leaves_the_answer_insufficient_scope(monkeypatch, acme, caplog):
    _Resolver(monkeypatch, initial=ManagedAuth(access_token="tok"))

    async def broken_note(cid, scopes):
        raise ConnectionError("store down")

    monkeypatch.setattr(ti, "note_scopes_wanted", broken_note)
    pool = _FakePool([_http_error(403, _SCOPE_CHALLENGE)])
    with caplog.at_level(logging.ERROR), pytest.raises(ConnectorInsufficientScopeError):
        await _call(_acme_entry(), pool)
    assert "noting the scopes connection" in caplog.text


def test_the_scope_error_is_an_unauthorized_kind():
    exc = ConnectorInsufficientScopeError(connection_id=CID, provider_id="acme", sub_service="mail", scopes=["a"])
    assert error_kind(exc) is ErrorKind.UNAUTHORIZED


def test_the_scope_error_becomes_the_insufficient_scope_result():
    exc = ConnectorInsufficientScopeError(
        connection_id=CID, provider_id="acme", sub_service="mail", scopes=["files:read", "files:write"]
    )
    payload = extract_connector_error_payload(ti.managed_auth_error_result(exc))
    assert payload is not None
    assert payload["code"] == "insufficient_scope"
    assert payload["scopes"] == ["files:read", "files:write"]
    assert payload["connection_id"] == CID
    assert payload["provider_id"] == "acme"
    assert payload["sub_service"] == "mail"
    assert "reconnect" in payload["message"]


# -- the provider's own server -----------------------------------------------------------------


@pytest.mark.parametrize("type_", ["http", None, "stdio"], ids=["type http", "no type", "type says stdio"])
async def test_a_call_to_another_url_is_refused_before_anything_is_resolved(monkeypatch, acme, type_):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="secret"))
    pool = _FakePool([_ok()])
    with pytest.raises(ConnectorConnectionError) as caught:
        await _call(_acme_entry(url="https://elsewhere.test/mcp", title="mine", type_=type_), pool)
    assert "remove the reference" in str(caught.value)
    assert "'mine'" in str(caught.value)
    assert resolver.resolves == []
    assert pool.requests == 0


_OAUTH_REPAIR = (
    "reconnect the account to rewrite the entry the connector wrote for that connection, "
    "or remove the reference from this entry if it is not that one"
)
_NO_AUTH_REPAIR = (
    "save the connection's services again to rewrite the entry the connector wrote for that connection, "
    "or remove the reference from this entry if it is not that one"
)


@pytest.mark.parametrize("title", ["acme_mail_work", "acme_mail_home", "mine"])
async def test_a_refused_entry_is_told_both_repairs_whatever_its_title(monkeypatch, acme, title):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="secret"))
    pool = _FakePool([_ok()])
    with pytest.raises(ConnectorConnectionError) as caught:
        await _call(_acme_entry(url="https://old.acme.test/mcp/mail", title=title), pool)
    assert str(caught.value) == (
        f"MCP entry {title!r} carries the reference of connection {CID} but its url is not the one provider "
        f"'acme' defines for sub-service 'mail', so no credential is sent to it; {_OAUTH_REPAIR}"
    )
    assert resolver.resolves == []
    assert pool.requests == 0


@pytest.mark.parametrize("title", ["httpsvc_main_main", "httpsvc_main_other"])
async def test_a_refused_no_auth_entry_is_told_to_save_its_services_or_remove_the_reference(monkeypatch, acme, title):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(headers={"token": "static"}))
    config = TaiMCPConfig(
        title=title,
        config=MCPConfig(type="http", url="https://old.httpsvc.test/mcp"),
        managed=ConnectorRef(connection_id=CID, provider_id="httpsvc", sub_service="main"),
    )
    pool = _FakePool([_ok()])
    with pytest.raises(ConnectorConnectionError) as caught:
        await _call(config, pool)
    assert str(caught.value) == (
        f"MCP entry {title!r} carries the reference of connection {CID} but its url is not the one provider "
        f"'httpsvc' defines for sub-service 'main', so no credential is sent to it; {_NO_AUTH_REPAIR}"
    )
    assert resolver.resolves == []
    assert pool.requests == 0


async def test_an_entry_of_an_unregistered_provider_is_told_to_register_it_or_remove_the_reference(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="secret"))
    config = TaiMCPConfig(
        title="ghost_mail_work",
        config=MCPConfig(type="http", url="https://acme.test/mcp/mail"),
        managed=ConnectorRef(connection_id=CID, provider_id="ghost", sub_service="mail"),
    )
    with pytest.raises(ConnectorConnectionError) as caught:
        await _call(config, _FakePool([]))
    assert str(caught.value) == (
        f"MCP entry 'ghost_mail_work' carries the reference of connection {CID} but provider 'ghost' is not "
        "registered, so no credential is sent to it; register provider 'ghost' again, or remove the reference "
        "from this entry"
    )
    assert resolver.resolves == []


async def test_an_entry_equal_to_the_builders_output_passes(monkeypatch, acme):
    from tai42_skeleton.connectors.service.manifest_writer import _build_entry

    _Resolver(monkeypatch, initial=ManagedAuth(access_token="secret"))
    entry = _build_entry(descriptor=acme, sub_service_id="mail", alias="work", connection_id=CID)
    pool = _FakePool([_ok()])
    out = await _call(entry, pool)
    assert out.isError is False
    assert pool.opened[0]["config"]["headers"]["authorization"] == "Bearer secret"


@pytest.mark.parametrize(
    "ref",
    [
        ConnectorRef(connection_id=CID, provider_id="acme", sub_service="nope"),
        ConnectorRef(connection_id=CID, provider_id="ghost", sub_service="mail"),
    ],
    ids=["unknown sub-service", "unknown provider"],
)
async def test_an_unknown_sub_service_or_provider_is_refused(monkeypatch, acme, ref):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="secret"))
    config = TaiMCPConfig(title="x", config=MCPConfig(type="http", url="https://acme.test/mcp/mail"), managed=ref)
    with pytest.raises(ConnectorConnectionError):
        await _call(config, _FakePool([]))
    assert resolver.resolves == []


async def test_a_refusal_is_logged_at_error(monkeypatch, acme, caplog):
    _Resolver(monkeypatch, initial=ManagedAuth(access_token="secret"))
    with caplog.at_level(logging.ERROR), pytest.raises(ConnectorConnectionError):
        await _call(_acme_entry(url="https://elsewhere.test/mcp", title="mine"), _FakePool([]))
    assert any(r.levelno == logging.ERROR and "'mine'" in r.getMessage() for r in caplog.records)


async def test_a_forced_refresh_of_a_mismatched_entry_refuses_before_any_resolve(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="secret"))
    with pytest.raises(ConnectorConnectionError):
        await ti._force_refresh(_acme_entry(url="https://elsewhere.test/mcp"), "secret")
    assert resolver.refreshes == []


async def test_resolving_a_mismatched_entry_refuses_before_any_resolve(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="secret"))
    with pytest.raises(ConnectorConnectionError):
        await resolve_managed_auth_for_config(_acme_entry(url="https://elsewhere.test/mcp"))
    assert resolver.resolves == []


async def test_evicting_a_mismatched_entry_closes_its_plain_key_and_resolves_nothing(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="secret"))
    config = _acme_entry(url="https://elsewhere.test/mcp")
    pool = _FakePool([])
    await ti.evict_pooled_session(config, "http", cast(FastMCPClient, pool))
    assert pool.closed == [config.model_dump()]
    assert resolver.resolves == []


async def test_evicting_an_entry_with_an_expired_stored_token_never_refreshes(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=None)  # the read-only resolve finds nothing fresh
    config = _acme_entry()
    pool = _FakePool([])
    await ti.evict_pooled_session(config, "http", cast(FastMCPClient, pool))
    assert resolver.resolves == [{"connection_id": CID, "allow_refresh": False}]
    assert resolver.refreshes == []
    assert pool.closed == [config.model_dump()]


async def test_evicting_an_entry_with_a_fresh_stored_token_closes_the_dispatch_key(monkeypatch, acme):
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="tok"))
    config = _acme_entry()
    pool = _FakePool([])
    await ti.evict_pooled_session(config, "http", cast(FastMCPClient, pool))
    assert resolver.resolves == [{"connection_id": CID, "allow_refresh": False}]
    assert pool.closed == [_effective_dump(config, ManagedAuth(access_token="tok"), "http")]


async def test_a_stdio_entry_is_not_checked(monkeypatch, acme):
    # A package-launched entry written with an older version: its args differ from the builder's
    # current output, and it still gets its per-call token.
    _Resolver(monkeypatch, initial=ManagedAuth(access_token="tok"))
    config = TaiMCPConfig(
        title="widgets_search_main",
        config=MCPConfig(type="stdio", command="uvx", args=["--from", "tai-mcp-widgets==1.0.0", "tai-mcp-widgets"]),
        managed=ConnectorRef(connection_id=CID, provider_id="widgets", sub_service="search"),
    )
    pool = _FakePool([])
    session = _FakeSession([_ok()])
    pool.seed(config.model_dump(), session)
    out = await _call(config, pool, "stdio")
    assert out.isError is False
    assert session.metas[0][0] == {CONNECTOR_META_TOKEN_KEY: "tok"}


# -- through the adapter, against a real loopback MCP server -----------------------------------


@pytest.fixture(scope="module")
def mcp_server() -> Iterator[mcp_fakes.FakeMcpServer]:
    yield from mcp_fakes.serve()


@pytest.fixture
def server(mcp_server: mcp_fakes.FakeMcpServer) -> Iterator[mcp_fakes.FakeMcpServer]:
    mcp_server.reset()
    yield mcp_server
    mcp_server.reset()


@pytest.fixture
async def closed_pools() -> AsyncIterator[None]:
    from tai42_kit.clients.base import shutdown_all_clients

    yield
    await shutdown_all_clients()


def _input_model():
    from pydantic import BaseModel

    class _NoArgs(BaseModel):
        pass

    return _NoArgs


async def _through_adapter(config: TaiMCPConfig):
    import tai42_skeleton.app.instance  # noqa: F401  (binds the app the adapter's monitoring reads)
    from tai42_skeleton.tools.adapters.mcp_tool_to_func import mcp_tool_call_wrapper

    return await mcp_tool_call_wrapper(config, "ping", _input_model(), {})


def _payload(result) -> dict:
    payload = extract_connector_error_payload(result)
    assert payload is not None, result
    return payload


@pytest.mark.usefixtures("closed_pools")
@pytest.mark.parametrize("type_", ["http", None, "stdio"], ids=["type http", "no type", "type says stdio"])
async def test_a_forged_entry_never_reaches_its_server(monkeypatch, acme, server, type_):
    _Resolver(monkeypatch, initial=ManagedAuth(access_token="secret"))
    result = await _through_adapter(_acme_entry(url=server.url, title="forged", type_=type_))
    payload = _payload(result)
    assert payload["code"] == "reconnect_required"
    assert "'forged'" in payload["message"]
    assert server.seen == []


@pytest.mark.usefixtures("closed_pools")
async def test_a_forged_no_auth_entry_never_reaches_its_server(monkeypatch, acme, server):
    _Resolver(monkeypatch, initial=ManagedAuth(headers={"x-api-key": "static-secret"}))
    config = TaiMCPConfig(
        title="forged",
        config=MCPConfig(type="http", url=server.url),
        managed=ConnectorRef(connection_id=CID, provider_id="httpsvc", sub_service="main"),
    )
    payload = _payload(await _through_adapter(config))
    assert payload["code"] == "reconnect_required"
    assert server.seen == []


def _register_acme_at(url: str) -> None:
    from tai42_contract.connectors.providers import McpServerDescriptor, SubServiceDescriptor

    descriptor = make_oauth_descriptor()
    descriptor.sub_services["mail"] = SubServiceDescriptor(
        id="mail", display_name="Mail", scopes=["mail.read"], mcp_server=McpServerDescriptor(type="http", url=url)
    )
    registry.reset_registry()
    registry.register_connector(descriptor)


@pytest.mark.usefixtures("closed_pools")
async def test_a_connect_time_401_through_the_adapter_refreshes_and_answers(monkeypatch, server):
    _register_acme_at(server.url)
    server.reset(mcp_fakes.bearer_only("fresh"))
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="stale"))
    result = await _through_adapter(_acme_entry(url=server.url))
    assert result == {"result": "pong"}
    assert resolver.refreshes == ["stale"]
    assert [s.authorization for s in server.initializes] == ["Bearer stale", "Bearer fresh"]


@pytest.mark.usefixtures("closed_pools")
async def test_a_refused_fresh_token_through_the_adapter_answers_auth_expired_as_a_value(monkeypatch, server):
    _register_acme_at(server.url)
    server.reset(mcp_fakes.bearer_only())
    _Resolver(monkeypatch, initial=ManagedAuth(access_token="stale"))
    result = await _through_adapter(_acme_entry(url=server.url))
    payload = _payload(result)
    assert payload["code"] == "auth_expired"
    # The fresh-session retry for an unreachable upstream is not taken for an auth failure.
    assert len(server.initializes) == 2


@pytest.mark.usefixtures("closed_pools")
async def test_a_scope_challenge_through_the_adapter_takes_no_fresh_session_retry(monkeypatch, server):
    _register_acme_at(server.url)
    server.reset(lambda token: mcp_fakes.insufficient_scope("files:read files:write"))
    resolver = _Resolver(monkeypatch, initial=ManagedAuth(access_token="tok"))
    payload = _payload(await _through_adapter(_acme_entry(url=server.url)))
    assert payload["code"] == "insufficient_scope"
    assert payload["scopes"] == ["files:read", "files:write"]
    assert resolver.refreshes == []
    assert len(server.initializes) == 1


# -- Import hygiene -----------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        "tai42_skeleton.connectors.token_injection",
        "tai42_skeleton.tools.adapters.mcp_tool_to_func",
    ],
)
def test_module_imports_alone_in_fresh_interpreter(module: str) -> None:
    """Each dispatch module must import cleanly as the FIRST skeleton import in a
    fresh interpreter. ``mcp_tool_to_func`` imports ``token_injection`` and both
    read ``settings.mcp_settings``; importing either one first must not re-enter a
    half-initialized peer. A subprocess gives the pristine ``sys.modules`` an
    in-process test (collected after the package graph is already warm) cannot."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
