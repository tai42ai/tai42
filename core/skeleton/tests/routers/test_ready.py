"""The ``/ready`` readiness route.

The route pings exactly the backing stores this deployment wired, deduped by
connection identity and pinged concurrently. These tests fake ``client_ctx`` so no
live Redis/Postgres is needed, and drive ``_wired_connections`` directly (or via a
stub) to isolate the response/dedupe/aggregation behavior from the real gates.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import cast

import pytest
from pydantic import SecretStr
from starlette.requests import Request
from tai42_contract.access_control import registry
from tai42_contract.access_control.identity import AuthIdentity, IdentityProvider, ReadinessTarget
from tai42_kit.clients import PostgresConnectionSettings, RedisConnectionSettings
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.plugins.quarantine import quarantine_plugin, reset_quarantine
from tai42_skeleton.routers import health


@pytest.fixture(autouse=True)
def _clean_quarantine():
    # The quarantine registry is process-global and owned by boot passes other
    # tests run; reset around each test so the count assertions are hermetic.
    reset_quarantine()
    yield
    reset_quarantine()


class _RedisBoom(Exception):
    """Distinct exception type so the test can assert the type name surfaces."""


class _FakeRedis:
    def __init__(self, fail_type: type[Exception] | None) -> None:
        self._fail_type = fail_type

    async def ping(self) -> None:
        if self._fail_type is not None:
            raise self._fail_type("secret-redis-host:6379 unreachable")


class _FakeConn:
    def __init__(self, fail_type: type[Exception] | None) -> None:
        self._fail_type = fail_type

    async def execute(self, sql: str) -> None:
        if self._fail_type is not None:
            raise self._fail_type("secret-pg-host:5432 unreachable")


class _FakePool:
    def __init__(self, fail_type: type[Exception] | None) -> None:
        self._fail_type = fail_type

    def connection(self):
        conn = _FakeConn(self._fail_type)

        @asynccontextmanager
        async def _cm():
            yield conn

        return _cm()


def _make_client_ctx(calls: list, fail_idents: frozenset = frozenset(), fail_type: type[Exception] = RuntimeError):
    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings):
        kwargs = settings.client_kwargs()
        ident = kwargs.get("url") or kwargs.get("dsn")
        calls.append((client_cls.__name__, ident))
        boom = fail_type if ident in fail_idents else None
        if client_cls is RedisClient:
            yield _FakeRedis(boom)
        else:
            yield _FakePool(boom)

    return fake_client_ctx


def _request() -> Request:
    # The handler ignores the request; a bare stand-in cast to Request suffices.
    return cast(Request, object())


async def test_ready_all_healthy_returns_200(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(health, "client_ctx", _make_client_ctx(calls))
    wired = [
        ("access_control", RedisClient, RedisConnectionSettings(redis_url="redis://ac")),
        ("tool_runs", RedisClient, RedisConnectionSettings(redis_url="redis://shared")),
        ("interactions", RedisClient, RedisConnectionSettings(redis_url="redis://shared")),
        # A Postgres connection needs a password to compose its DSN dedup key (no
        # hidden default), so the wired versioning target supplies one.
        ("versioning", PostgresClient, PostgresConnectionSettings(pg_host="db", pg_password=SecretStr("pw"))),
    ]
    monkeypatch.setattr(health, "_wired_connections", lambda: wired)

    resp = await health.readiness_check(_request())

    assert resp.status_code == 200
    body = json.loads(bytes(resp.body))
    assert body["status"] == "ready"
    assert body["checks"] == {
        "access_control": "ok",
        "tool_runs": "ok",
        "interactions": "ok",
        "versioning": "ok",
    }


async def test_ready_failure_returns_503_type_only(monkeypatch, caplog) -> None:
    calls: list = []
    monkeypatch.setattr(
        health,
        "client_ctx",
        _make_client_ctx(calls, fail_idents=frozenset({"redis://shared"}), fail_type=_RedisBoom),
    )
    wired = [
        ("access_control", RedisClient, RedisConnectionSettings(redis_url="redis://ac")),
        ("tool_runs", RedisClient, RedisConnectionSettings(redis_url="redis://shared")),
        ("interactions", RedisClient, RedisConnectionSettings(redis_url="redis://shared")),
    ]
    monkeypatch.setattr(health, "_wired_connections", lambda: wired)

    with caplog.at_level("WARNING", logger=health.logger.name):
        resp = await health.readiness_check(_request())

    assert resp.status_code == 503
    raw = bytes(resp.body)
    body = json.loads(raw)
    assert body["status"] == "not_ready"
    assert body["checks"]["access_control"] == "ok"
    # Both subsystems sharing the failed connection fail together, carrying only
    # the exception TYPE name.
    assert body["checks"]["tool_runs"] == "_RedisBoom"
    assert body["checks"]["interactions"] == "_RedisBoom"
    # The exception MESSAGE never reaches the public body...
    assert b"secret-redis-host" not in raw
    # ...but the full detail is in the logs, exactly one warning for the one failed
    # distinct connection.
    assert "secret-redis-host" in caplog.text
    assert sum("readiness ping failed" in rec.getMessage() for rec in caplog.records) == 1


async def test_ready_dedupes_shared_connection(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(health, "client_ctx", _make_client_ctx(calls))
    wired = [
        ("tool_runs", RedisClient, RedisConnectionSettings(redis_url="redis://shared")),
        ("interactions", RedisClient, RedisConnectionSettings(redis_url="redis://shared")),
    ]
    monkeypatch.setattr(health, "_wired_connections", lambda: wired)

    resp = await health.readiness_check(_request())

    assert resp.status_code == 200
    # One ping for the single shared connection, though two subsystems use it.
    assert len(calls) == 1
    body = json.loads(bytes(resp.body))
    assert body["checks"] == {"tool_runs": "ok", "interactions": "ok"}


async def test_wired_connections_gates_out_pg_and_inmemory_hooks(monkeypatch) -> None:
    # connectors + versioning not wired, hooks in-memory: no Postgres check and no
    # hooks check are produced. Hermetic against ambient PG passwords — the
    # marketplace/tool_meta rows are password-gated, so an ambient
    # MARKETPLACE_STORE_PG_PASSWORD / TOOL_META_STORE_PG_PASSWORD / TAI_DEFAULT_PG_PASSWORD
    # would otherwise leak a PostgresClient row and flip the assertion below.
    _quiet_stores(monkeypatch)
    for key in list(os.environ):
        if key.startswith("HOOKS_"):
            monkeypatch.delenv(key, raising=False)

    reset_all_settings()
    try:
        conns = health._wired_connections()
    finally:
        reset_all_settings()

    names = [name for name, _, _ in conns]
    classes = [cls for _, cls, _ in conns]
    assert "connectors" not in names
    assert "versioning" not in names
    assert "hooks" not in names
    assert PostgresClient not in classes


async def test_wired_connections_gates_in_stores_when_wired(monkeypatch) -> None:
    _quiet_stores(monkeypatch)
    monkeypatch.setattr(health.instance, "versioned_store_in_use", lambda: True)
    # A connector store password wires connectors in; a connector Redis URL rides the
    # additional Redis row on top of the Postgres row.
    monkeypatch.setenv("CONNECTOR_STORE_PG_PASSWORD", "pw")
    monkeypatch.setenv("CONNECTOR_STORE_REDIS_URL", "redis://connectors")
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://hooks")
    reset_all_settings()
    try:
        conns = health._wired_connections()
    finally:
        monkeypatch.delenv("CONNECTOR_STORE_REDIS_URL", raising=False)
        reset_all_settings()

    names = [name for name, _, _ in conns]
    classes = [cls for _, cls, _ in conns]
    assert "connectors" in names
    assert "versioning" in names
    assert "hooks" in names
    # connectors contributes both a Postgres and a Redis connection here; versioning
    # contributes a Postgres one.
    connector_classes = {cls for name, cls, _ in conns if name == "connectors"}
    assert connector_classes == {PostgresClient, RedisClient}
    assert PostgresClient in classes


async def test_ready_connectors_pg_only_readies_without_connector_redis(monkeypatch) -> None:
    # The connectors split: a store password wires the Postgres row, but with no
    # connector Redis URL resolving (own var or TAI_DEFAULT_REDIS_URL), no Redis row is
    # emitted — so a Postgres-only connector deploy readies (200) instead of 503ing on a
    # Redis it never wired. Mirrors the tool_runs/interactions redis-url gate shape.
    calls: list = []
    monkeypatch.setattr(health, "client_ctx", _make_client_ctx(calls))
    _quiet_stores(monkeypatch)
    # A password wires connectors on; a host lets the Postgres row compose its DSN.
    monkeypatch.setenv("CONNECTOR_STORE_PG_PASSWORD", "pw")
    monkeypatch.setenv("CONNECTOR_STORE_PG_HOST", "db")
    reset_all_settings()
    try:
        wired = health._wired_connections()
        resp = await health.readiness_check(_request())
    finally:
        reset_all_settings()

    rows = {(name, cls) for name, cls, _ in wired}
    assert ("connectors", PostgresClient) in rows
    assert ("connectors", RedisClient) not in rows
    assert resp.status_code == 200
    body = json.loads(bytes(resp.body))
    assert body["checks"]["connectors"] == "ok"


async def test_wired_connections_gates_sub_mcp_on_redis_url(monkeypatch) -> None:
    # The durable sub-MCP registration store joins the readiness set exactly when
    # SUB_MCP_REDIS_URL is set (same gate shape as hooks): a ``sub_mcp`` Redis check
    # appears with it set, and no ``sub_mcp`` check appears with it unset.
    _quiet_stores(monkeypatch)
    for key in list(os.environ):
        if key.startswith("SUB_MCP_"):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("SUB_MCP_REDIS_URL", "redis://sub-mcp")
    reset_all_settings()
    try:
        wired = health._wired_connections()
    finally:
        reset_all_settings()
    sub_mcp = [(name, cls) for name, cls, _ in wired if name == "sub_mcp"]
    assert sub_mcp == [("sub_mcp", RedisClient)]

    monkeypatch.delenv("SUB_MCP_REDIS_URL", raising=False)
    reset_all_settings()
    try:
        wired_unset = health._wired_connections()
    finally:
        reset_all_settings()
    assert "sub_mcp" not in [name for name, _, _ in wired_unset]


def _quiet_stores(monkeypatch) -> None:
    # Keep the readiness set to just the feature under test: no connectors/versioning,
    # and none of the shared TAI_DEFAULT_* / feature stores leaking a row in. Connectors
    # gates on its store password (own var or TAI_DEFAULT_PG_PASSWORD), so both are cleared.
    monkeypatch.setattr(health.instance, "versioned_store_in_use", lambda: False)
    for var in (
        "TAI_DEFAULT_REDIS_URL",
        "TAI_DEFAULT_PG_PASSWORD",
        "TAI_TOOL_RUNS_REDIS_URL",
        "INTERACTIONS_REDIS_URL",
        "MARKETPLACE_STORE_PG_PASSWORD",
        "TOOL_META_STORE_PG_PASSWORD",
        "CONNECTOR_STORE_PG_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)


async def test_wired_connections_gates_marketplace_and_tool_meta_on_pg_password(monkeypatch) -> None:
    # the marketplace + tool-metadata Postgres stores join the readiness set
    # exactly when their password is configured (own var or TAI_DEFAULT_PG_PASSWORD).
    _quiet_stores(monkeypatch)
    monkeypatch.setenv("MARKETPLACE_STORE_PG_PASSWORD", "pw")
    monkeypatch.setenv("TOOL_META_STORE_PG_PASSWORD", "pw")
    reset_all_settings()
    try:
        wired = health._wired_connections()
    finally:
        reset_all_settings()
    rows = {(name, cls) for name, cls, _ in wired}
    assert ("marketplace", PostgresClient) in rows
    assert ("tool_meta", PostgresClient) in rows


async def test_wired_connections_omits_marketplace_and_tool_meta_when_unconfigured(monkeypatch) -> None:
    _quiet_stores(monkeypatch)
    reset_all_settings()
    try:
        names = [name for name, _, _ in health._wired_connections()]
    finally:
        reset_all_settings()
    assert "marketplace" not in names
    assert "tool_meta" not in names


async def test_wired_connections_omits_tool_runs_and_interactions_when_redis_unset(monkeypatch) -> None:
    # with no localhost default, an unconfigured tool-runs / interactions Redis
    # contributes no readiness row (the feature is cleanly OFF).
    _quiet_stores(monkeypatch)
    reset_all_settings()
    try:
        names = [name for name, _, _ in health._wired_connections()]
    finally:
        reset_all_settings()
    assert "tool_runs" not in names
    assert "interactions" not in names


async def test_wired_connections_gates_rate_limit_on_trigger_only(monkeypatch) -> None:
    # The rate-limit readiness row rides the FULL enable disjunction, including the
    # trigger door: with webhook + interactions-callback disabled and ONLY trigger
    # enabled, a configured rate-limit Redis still contributes the rate_limit ping.
    # (A regression dropping ``trigger_enabled`` from the disjunction would gate the
    # row out here and fail this test.)
    _quiet_stores(monkeypatch)
    monkeypatch.setenv("TAI_RATE_LIMIT_WEBHOOK_ENABLED", "false")
    monkeypatch.setenv("TAI_RATE_LIMIT_INTERACTIONS_CALLBACK_ENABLED", "false")
    monkeypatch.setenv("TAI_RATE_LIMIT_TRIGGER_ENABLED", "true")
    monkeypatch.setenv("TAI_RATE_LIMIT_REDIS_URL", "redis://rl")
    reset_all_settings()
    try:
        wired = health._wired_connections()
    finally:
        monkeypatch.delenv("TAI_RATE_LIMIT_REDIS_URL", raising=False)
        reset_all_settings()

    rate_limit = [(name, cls) for name, cls, _ in wired if name == "rate_limit"]
    assert rate_limit == [("rate_limit", RedisClient)]


async def test_wired_connections_enumerates_identity_provider_generically(monkeypatch) -> None:
    # Core enumerates the ACTIVE identity provider's declared readiness target(s)
    # through the IdentityProvider ABC — it does NOT string-match the provider name
    # "redis". A provider registered under any OTHER name, declaring any store, is
    # health-checked all the same.
    declared = RedisConnectionSettings(redis_url="redis://custom-idp-store")

    class _CustomProvider(IdentityProvider):
        def __init__(self, settings: object) -> None:
            self._settings = settings

        async def validate_token(self, token: str) -> AuthIdentity | None:
            return None

        def readiness_targets(self) -> tuple[ReadinessTarget, ...]:
            return (ReadinessTarget("access_control", RedisClient, declared),)

    registry.register_identity_provider("custom_idp", _CustomProvider)
    monkeypatch.setenv("ACCESS_CONTROL_AUTH_PROVIDERS", '["custom_idp"]')
    reset_all_settings()
    try:
        conns = health._wired_connections()
    finally:
        registry._REGISTRY.pop("custom_idp", None)
        reset_all_settings()

    # The provider registered under "custom_idp" (never "redis") still contributes its
    # declared target, verbatim — proving core routes through the ABC, not a name match.
    ac = [conn for conn in conns if conn[0] == "access_control"]
    assert ac == [("access_control", RedisClient, declared)]
    assert ac[0][2] is declared


async def test_ready_nothing_wired_returns_200_empty(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(health, "client_ctx", _make_client_ctx(calls))
    monkeypatch.setattr(health, "_wired_connections", list)

    resp = await health.readiness_check(_request())

    assert resp.status_code == 200
    body = json.loads(bytes(resp.body))
    assert body == {"status": "ready", "checks": {}, "plugin_quarantine": 0}
    assert calls == []


async def test_ready_quarantine_count_rides_along_without_flipping_red(monkeypatch) -> None:
    # A quarantined plugin is DIAGNOSTIC: the worker is serving, so readiness
    # stays green and only the count changes — rotating the worker would turn
    # one broken plugin into a fleet outage.
    calls: list = []
    monkeypatch.setattr(health, "client_ctx", _make_client_ctx(calls))
    monkeypatch.setattr(health, "_wired_connections", list)
    reset_quarantine()
    try:
        quarantine_plugin("acme_plugin", "tools module failed to import: boom")
        resp = await health.readiness_check(_request())
    finally:
        reset_quarantine()

    assert resp.status_code == 200
    body = json.loads(bytes(resp.body))
    assert body["status"] == "ready"
    assert body["plugin_quarantine"] == 1
