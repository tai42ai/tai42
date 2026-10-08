"""Checkpoint factory: provider selection, conn-string resolution, the
benign-setup-race guard, redis client ownership, and the serialization guard.

The memory backend is real (in-process, no I/O). The redis/sqlite/postgres
backends are mocked at their import seam — sqlite/postgres modules are injected
into sys.modules (their extras are not installed), redis attributes are patched
on the real module. No real Redis/Postgres/SQLite connection is opened.
"""

import os
import sys
import types
from typing import Any

import pytest

pytest.importorskip("langgraph")

from tai42_kit.llm.checkpoint import checkpoint as cp

from .conftest import client_target, install_fake_named_pool, install_spy_client


@pytest.fixture(autouse=True)
def _no_store_side_effects(monkeypatch):
    """Keep the resource builders off the store-format gate and the SQL ledgers (tested on their own)."""

    async def _no_gate(resource):
        return None

    class _FakeSqlLedger:
        def __init__(self, handle):
            self.handle = handle

        async def setup(self):
            pass

    monkeypatch.setattr(cp, "ensure_store_format", _no_gate)
    monkeypatch.setattr(cp, "SqliteFinishedThreadLedger", _FakeSqlLedger)
    monkeypatch.setattr(cp, "PostgresFinishedThreadLedger", _FakeSqlLedger)


def _use_fake_redis_saver(monkeypatch, saver_cls):
    """Build the redis checkpoint on ``saver_cls`` in place of the kit's guarded Redis saver."""
    from tai42_kit.llm.checkpoint import codec

    if not hasattr(saver_cls, "serde"):
        saver_cls.serde = None
    monkeypatch.setattr(codec, "GuardedAsyncRedisSaver", saver_cls)


# --------------------------------------------------------------------------- #
# create_checkpoint_resource — provider branches
# --------------------------------------------------------------------------- #
async def test_memory_resource_returns_saver_and_noop_close():
    from langgraph.checkpoint.memory import InMemorySaver

    resource, closer = await cp.create_checkpoint_resource("memory")
    assert resource.provider == "memory"
    assert isinstance(resource.handle, InMemorySaver)
    assert resource.redis_client is None
    await closer()  # no-op, must not raise


async def test_unsupported_provider_raises():
    from tai42_kit.llm.checkpoint.providers import UnknownCheckpointProviderError

    with pytest.raises(UnknownCheckpointProviderError, match="Unsupported checkpoint provider: 'bogus'"):
        await cp.create_checkpoint_resource("bogus")


async def test_sqlite_requires_conn_string():
    with pytest.raises(ValueError, match="sqlite checkpoint provider requires"):
        await cp.create_checkpoint_resource("sqlite", None)


async def test_postgres_none_conn_string_raises_named_error_without_identity():
    # None + postgres falls back to the base Postgres DSN, which raises a named
    # error naming PG_HOST when its identity is unset (env is cleared by the
    # autouse suite fixture).
    with pytest.raises(ValueError, match="Postgres connection is not configured"):
        await cp.create_checkpoint_resource("postgres", None)


async def test_redis_none_conn_string_raises_named_error_without_url(monkeypatch):
    # None + redis falls back to the base Redis URL; with none configured the
    # named error fires BEFORE the langgraph import (a fake module without
    # AsyncRedisSaver would fail to import, so reaching the ValueError proves the
    # guard runs first).
    fake_mod: Any = types.ModuleType("langgraph.checkpoint.redis")
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.redis", fake_mod)
    with pytest.raises(ValueError, match="LLM_PROVIDER_CHECKPOINT_CONN_STRING") as excinfo:
        await cp.create_checkpoint_resource("redis", None)
    message = str(excinfo.value)
    assert message == cp.REDIS_CHECKPOINT_NOT_CONFIGURED_MESSAGE
    assert message == (
        "the Redis checkpoint is not configured: set "
        "LLM_PROVIDER_CHECKPOINT_CONN_STRING (or the base Redis URL "
        "REDIS_URL / TAI_DEFAULT_REDIS_URL). The target Redis must provide "
        "the JSON and search modules (RedisJSON + RediSearch); a plain Redis "
        "fails mid-run on FT.* commands."
    )


async def test_redis_none_conn_string_resolves_from_tai_default(monkeypatch):
    # None + redis + a TAI_DEFAULT_REDIS_URL (and NO bare REDIS_URL — the autouse
    # env fixture clears it) resolves the checkpoint end-to-end from the shared
    # namespace: the client the saver is handed is pointed at that resolved URL.
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")
    assert "REDIS_URL" not in os.environ
    setups = []

    class _FakeSaver:
        # Keyword-only ``redis_client``, no ``redis_url``: a kit that went back to
        # handing over a URL would not construct.
        def __init__(self, *, redis_client, ttl=None):
            self.redis_client = redis_client

        async def asetup(self):
            setups.append(client_target(self.redis_client))

        async def __aexit__(self, *exc):
            pass

    _use_fake_redis_saver(monkeypatch, _FakeSaver)

    resource, closer = await cp.create_checkpoint_resource("redis", None)
    assert client_target(resource.handle.redis_client) == "shared:6379/0"
    assert resource.redis_client is resource.handle.redis_client
    assert setups == ["shared:6379/0"]
    await closer()


async def test_postgres_none_conn_string_resolves_from_base_pg_settings(monkeypatch):
    # None + postgres resolves to the base Postgres DSN, so the pool is opened
    # against the identity from the base ``PG_*`` namespace.
    monkeypatch.setenv("PG_HOST", "shared-db")
    monkeypatch.setenv("PG_PASSWORD", "shared-secret")
    captured, _closed = install_fake_named_pool(monkeypatch)

    class _FakeSaver:
        def __init__(self, conn):
            pass

        async def setup(self):
            pass

    saver_mod: Any = types.ModuleType("langgraph.checkpoint.postgres.aio")
    saver_mod.AsyncPostgresSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", saver_mod)

    _resource, closer = await cp.create_checkpoint_resource("postgres", None)
    assert captured["conninfo"].startswith("postgresql://")
    assert "shared-db" in captured["conninfo"]
    # The base-namespace pool is named for the checkpoint role, sized from the base
    # Postgres deployment settings, and its fill is awaited here through the shared seam.
    assert captured["name"] == "postgres@shared-db/postgres:checkpoint"
    assert captured["min_size"] == 2
    assert captured["max_size"] == 10
    assert captured["open_wait"] is True
    await closer()


async def test_redis_resource_setup_called(monkeypatch):
    setups = []

    class _FakeSaver:
        def __init__(self, *, redis_client, ttl=None):
            self.redis_client = redis_client
            self.ttl = ttl
            self.closed = False

        async def asetup(self):
            setups.append(client_target(self.redis_client))

        async def __aexit__(self, *exc):
            self.closed = True

    _use_fake_redis_saver(monkeypatch, _FakeSaver)

    resource, closer = await cp.create_checkpoint_resource("redis", "redis://h:6379/0")
    assert isinstance(resource.handle, _FakeSaver)
    assert setups == ["h:6379/0"]
    await closer()
    # The closer tears the saver down (releasing its indexes), not a no-op.
    assert resource.handle.closed is True


async def test_redis_setup_ignores_already_exists(monkeypatch):
    class _FakeSaver:
        def __init__(self, *, redis_client, ttl=None):
            pass

        async def asetup(self):
            raise RuntimeError("Index already exists")

    _use_fake_redis_saver(monkeypatch, _FakeSaver)

    resource, _ = await cp.create_checkpoint_resource("redis", "redis://h/0")
    assert isinstance(resource.handle, _FakeSaver)


async def test_redis_setup_reraises_other_errors(monkeypatch):
    closed = []

    class _FakeSaver:
        def __init__(self, *, redis_client, ttl=None):
            pass

        async def asetup(self):
            raise RuntimeError("connection refused")

        async def __aexit__(self, *exc):
            closed.append(True)

    _use_fake_redis_saver(monkeypatch, _FakeSaver)

    with pytest.raises(RuntimeError, match="connection refused"):
        await cp.create_checkpoint_resource("redis", "redis://h/0")
    # A non-benign setup failure tears down the saver we opened, not a leak.
    assert closed == [True]


# --------------------------------------------------------------------------- #
# redis client injection + ownership
# --------------------------------------------------------------------------- #
async def test_redis_saver_is_handed_a_client_built_from_the_resolved_url(monkeypatch):
    # The kit resolves the URL and injects the CLIENT. Nothing downstream is left
    # to re-resolve a connection from the bare ``REDIS_URL`` env var, which is
    # absent here — only the conn string names the target.
    captured: dict[str, Any] = {}

    class _FakeSaver:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def asetup(self):
            pass

        async def __aexit__(self, *exc):
            pass

    _use_fake_redis_saver(monkeypatch, _FakeSaver)

    assert "REDIS_URL" not in os.environ
    await cp.create_checkpoint_resource("redis", "rediss://user:pw@vault:6380/3")

    # A client, never a URL — and no ``connection_args``, which the saver reads
    # only while building a client of its own.
    assert set(captured) == {"redis_client", "ttl"}
    from redis.asyncio import Redis as AsyncRedis
    from redis.asyncio.connection import SSLConnection

    client = captured["redis_client"]
    assert isinstance(client, AsyncRedis)
    assert client_target(client) == "vault:6380/3"
    # The WHOLE url survives the hand-off, not just the endpoint: ``rediss://``
    # selects the TLS connection class (identity, not isinstance — the plain
    # ``Connection`` is its base) and the credentials ride along with it.
    pool = client.connection_pool
    assert pool.connection_class is SSLConnection
    assert pool.connection_kwargs["username"] == "user"
    assert pool.connection_kwargs["password"] == "pw"


async def test_real_redis_saver_never_closes_an_injected_client():
    # The upstream contract the kit's ownership rests on: handed a client, the
    # saver is not its owner and its teardown leaves it open — so the kit must
    # close it, and closing it is not a double-close. Construction and teardown
    # of a non-owning saver do no I/O, so no live Redis is needed.
    saver_mod = pytest.importorskip("langgraph.checkpoint.redis")
    from redis.asyncio import Redis as AsyncRedis

    closes = []
    client = AsyncRedis.from_url("redis://127.0.0.1:6379/0")

    async def _spy_aclose(*args: Any, **kwargs: Any) -> None:
        closes.append(True)

    client.aclose = _spy_aclose  # type: ignore[method-assign]

    saver = saver_mod.AsyncRedisSaver(redis_client=client, ttl=None)
    await saver.__aexit__(None, None, None)
    assert closes == []


async def test_redis_closer_closes_the_injected_client_exactly_once(monkeypatch):
    built = install_spy_client(monkeypatch)
    exits = []

    class _FakeSaver:
        def __init__(self, *, redis_client, ttl=None):
            pass

        async def asetup(self):
            pass

        async def __aexit__(self, *exc):
            exits.append(True)

    _use_fake_redis_saver(monkeypatch, _FakeSaver)

    _resource, closer = await cp.create_checkpoint_resource("redis", "redis://h/0")
    assert len(built) == 1
    assert built[0].closes == 0  # nothing closes the live resource

    await closer()
    # One cleanup path: the saver's teardown runs, then the kit closes the client
    # it built — once, never twice.
    assert exits == [True]
    assert built[0].closes == 1


async def test_redis_setup_failure_closes_the_injected_client(monkeypatch):
    built = install_spy_client(monkeypatch)

    class _FakeSaver:
        def __init__(self, *, redis_client, ttl=None):
            pass

        async def asetup(self):
            raise RuntimeError("connection refused")

        async def __aexit__(self, *exc):
            pass

    _use_fake_redis_saver(monkeypatch, _FakeSaver)

    with pytest.raises(RuntimeError, match="connection refused"):
        await cp.create_checkpoint_resource("redis", "redis://h/0")
    # No cleanup fn is returned on this path, so the client the kit built must be
    # closed here or it leaks.
    assert built[0].closes == 1


async def test_redis_client_closes_even_when_saver_teardown_raises(monkeypatch):
    built = install_spy_client(monkeypatch)

    class _FakeSaver:
        def __init__(self, *, redis_client, ttl=None):
            pass

        async def asetup(self):
            pass

        async def __aexit__(self, *exc):
            raise RuntimeError("teardown blew up")

    _use_fake_redis_saver(monkeypatch, _FakeSaver)

    _resource, closer = await cp.create_checkpoint_resource("redis", "redis://h/0")
    with pytest.raises(RuntimeError, match="teardown blew up"):
        await closer()
    assert built[0].closes == 1


def _install_fake_redis_saver(monkeypatch) -> dict[str, Any]:
    """Install a fake ``AsyncRedisSaver`` that records the ``ttl`` it was built with."""
    captured: dict[str, Any] = {}

    class _FakeSaver:
        def __init__(self, *, redis_client, ttl=None):
            captured["ttl"] = ttl

        async def asetup(self):
            pass

        async def __aexit__(self, *exc):
            pass

    _use_fake_redis_saver(monkeypatch, _FakeSaver)
    return captured


async def test_redis_ttl_is_the_waiting_retention_set_at_write(monkeypatch):
    # Every key a write stamps carries the waiting retention; a read never re-arms it.
    from tai42_kit.settings import reset_all_settings

    captured = _install_fake_redis_saver(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES", "2880")
    reset_all_settings()
    try:
        await cp.create_checkpoint_resource("redis", "redis://h/0")
    finally:
        reset_all_settings()
    assert captured["ttl"] == {"default_ttl": 2880, "refresh_on_read": False}


async def test_redis_ttl_default_is_seven_days(monkeypatch):
    from tai42_kit.settings import reset_all_settings

    captured = _install_fake_redis_saver(monkeypatch)
    reset_all_settings()
    try:
        await cp.create_checkpoint_resource("redis", "redis://h/0")
    finally:
        reset_all_settings()
    assert captured["ttl"] == {"default_ttl": 10080, "refresh_on_read": False}


async def test_sqlite_resource_builds_and_closes(monkeypatch):
    closed = []

    class _FakeConn:
        async def close(self):
            closed.append(True)

    class _FakeSaver:
        def __init__(self, conn):
            self.conn = conn

        async def setup(self):
            pass

    aiosqlite_mod: Any = types.ModuleType("aiosqlite")

    async def _connect(path):
        return _FakeConn()

    aiosqlite_mod.connect = _connect
    saver_mod: Any = types.ModuleType("langgraph.checkpoint.sqlite.aio")
    saver_mod.AsyncSqliteSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "aiosqlite", aiosqlite_mod)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite.aio", saver_mod)

    resource, closer = await cp.create_checkpoint_resource("sqlite", "/tmp/x.db")
    assert isinstance(resource.handle, _FakeConn)
    assert resource.ledger.handle is resource.handle  # pyright: ignore[reportAttributeAccessIssue]
    await closer()
    assert closed == [True]


async def test_postgres_resource_builds_and_closes(monkeypatch):
    captured_pool_kwargs, closed = install_fake_named_pool(monkeypatch)

    class _FakeSaver:
        def __init__(self, conn):
            pass

        async def setup(self):
            pass

    saver_mod: Any = types.ModuleType("langgraph.checkpoint.postgres.aio")
    saver_mod.AsyncPostgresSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", saver_mod)

    resource, closer = await cp.create_checkpoint_resource("postgres", "postgresql://u@h/db")
    # The saver's required connection kwargs are handed to the pool.
    from psycopg.rows import dict_row

    assert captured_pool_kwargs["kwargs"] == {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}
    # An explicit DSN gets the kit's explicit-DSN pool sizes.
    assert captured_pool_kwargs["min_size"] == 1
    assert captured_pool_kwargs["max_size"] == 20
    assert resource.ledger.handle is resource.handle  # pyright: ignore[reportAttributeAccessIssue]
    # Named for the checkpoint role, and its fill is awaited here rather than in
    # background workers.
    assert captured_pool_kwargs["name"] == "postgres@h/db:checkpoint"
    assert captured_pool_kwargs["open_wait"] is True
    await closer()
    assert closed == [True]


async def test_sqlite_resource_closes_conn_on_setup_failure(monkeypatch):
    # setup() failing must not leak the connection opened just before it: no
    # cleanup fn is returned on this path, so the branch closes it itself.
    closed = []

    class _FakeConn:
        async def close(self):
            closed.append(True)

    class _FakeSaver:
        def __init__(self, conn):
            pass

        async def setup(self):
            raise RuntimeError("setup boom")

    aiosqlite_mod: Any = types.ModuleType("aiosqlite")

    async def _connect(path):
        return _FakeConn()

    aiosqlite_mod.connect = _connect
    saver_mod: Any = types.ModuleType("langgraph.checkpoint.sqlite.aio")
    saver_mod.AsyncSqliteSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "aiosqlite", aiosqlite_mod)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite.aio", saver_mod)

    with pytest.raises(RuntimeError, match="setup boom"):
        await cp.create_checkpoint_resource("sqlite", "/tmp/x.db")
    assert closed == [True]


async def test_postgres_resource_closes_pool_on_setup_failure(monkeypatch):
    # setup() failing must not leak the opened pool: no cleanup fn is returned on this
    # path, so the branch closes the pool itself.
    _captured, closed = install_fake_named_pool(monkeypatch)

    class _FakeSaver:
        def __init__(self, conn):
            pass

        async def setup(self):
            raise RuntimeError("setup boom")

    saver_mod: Any = types.ModuleType("langgraph.checkpoint.postgres.aio")
    saver_mod.AsyncPostgresSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", saver_mod)

    with pytest.raises(RuntimeError, match="setup boom"):
        await cp.create_checkpoint_resource("postgres", "postgresql://u@h/db")
    assert closed == [True]


# --------------------------------------------------------------------------- #
# get_saver_from_resource
# --------------------------------------------------------------------------- #
class _FakeSaver:
    """A stand-in saver carrying the ``serde`` the serialization guard wraps."""

    def __init__(self, resource=None):
        self.resource = resource
        self.serde = object()


def _resource(provider, handle):
    return cp.CheckpointResource(provider, handle, ledger=object())  # type: ignore[arg-type]


def test_get_saver_memory_and_redis_return_the_resource_handle():
    # The same handle object is returned (its serde is guarded in place), so the
    # registry's cached resource identity is preserved.
    memory_saver = _FakeSaver()
    redis_saver = _FakeSaver()
    assert cp.get_saver_from_resource("memory", _resource("memory", memory_saver)) is memory_saver
    assert cp.get_saver_from_resource("redis", _resource("redis", redis_saver)) is redis_saver
    assert isinstance(memory_saver.serde, cp._GuardedSerializer)
    assert isinstance(redis_saver.serde, cp._GuardedSerializer)


def test_get_saver_sqlite_wraps_resource(monkeypatch):
    saver_mod: Any = types.ModuleType("langgraph.checkpoint.sqlite.aio")
    saver_mod.AsyncSqliteSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite.aio", saver_mod)
    out = cp.get_saver_from_resource("sqlite", _resource("sqlite", "conn"))
    assert isinstance(out, _FakeSaver)
    assert out.resource == "conn"
    # The saver the graph checkpoints through has its serializer guarded, with no codec.
    assert isinstance(out.serde, cp._GuardedSerializer)
    assert out.serde._codec is None


def test_get_saver_postgres_wraps_resource_with_the_blob_codec(monkeypatch):
    from tai42_kit.llm.checkpoint.codec import BlobCompressCodec

    saver_mod: Any = types.ModuleType("langgraph.checkpoint.postgres.aio")
    saver_mod.AsyncPostgresSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", saver_mod)
    out = cp.get_saver_from_resource("postgres", _resource("postgres", "pool"))
    assert isinstance(out, _FakeSaver)
    assert out.resource == "pool"
    assert isinstance(out.serde, cp._GuardedSerializer)
    assert isinstance(out.serde._codec, BlobCompressCodec)


def test_get_saver_redis_installs_the_faithful_codec():
    from tai42_kit.llm.checkpoint.codec import FaithfulRedisSerializer, RedisFaithfulCodec

    saver = _FakeSaver()
    cp.get_saver_from_resource("redis", _resource("redis", saver))
    serde: Any = saver.serde
    assert isinstance(serde._inner, FaithfulRedisSerializer)
    assert isinstance(serde._codec, RedisFaithfulCodec)


def test_get_saver_unknown_provider_raises():
    from tai42_kit.llm.checkpoint.providers import UnknownCheckpointProviderError

    with pytest.raises(UnknownCheckpointProviderError, match="Unsupported checkpoint provider: 'bogus'"):
        cp.get_saver_from_resource("bogus", _resource("bogus", object()))


def test_get_saver_refuses_a_resource_of_another_provider():
    with pytest.raises(ValueError, match="of provider 'memory' cannot serve provider 'redis'"):
        cp.get_saver_from_resource("redis", _resource("memory", _FakeSaver()))


# --------------------------------------------------------------------------- #
# Serialization guard — msgpack encode failure becomes a loud typed error
# --------------------------------------------------------------------------- #
# ormsgpack encodes an integer natively only within [-2**63, 2**64-1]; an integer
# past that has no msgpack integer encoding and aborts the serializer. These are
# the checkpoint doors the schema-level int64 injection cannot reach — a plain
# tool-call argument and a tool result routed into flow state.
_OVERSIZED_INT = 2**64


def _guarded_memory_serde():
    from langgraph.checkpoint.memory import InMemorySaver

    return cp.get_saver_from_resource("memory", _resource("memory", InMemorySaver())).serde


def test_guard_tool_call_args_overflow_raises_typed_error_naming_path():
    # The plain tool-call-args door: an oversized integer buried in a tool call's
    # args aborts msgpack; the guard names the exact path instead of crashing.
    serde = _guarded_memory_serde()
    value = {"messages": [{"tool_calls": [{"args": {"count": _OVERSIZED_INT}}]}]}
    with pytest.raises(cp.CheckpointSerializationError) as excinfo:
        serde.dumps_typed(value)
    message = str(excinfo.value)
    assert "['messages'][0]['tool_calls'][0]['args']['count']" in message
    assert str(_OVERSIZED_INT) in message


def test_guard_tool_result_into_flow_state_overflow_raises_typed_error_naming_path():
    # The tool-result-into-flow-state door: an oversized integer inside a tool
    # result routed onto a flow-state channel aborts msgpack; the guard names it.
    serde = _guarded_memory_serde()
    value = {"flow_state": {"result": [0, {"total": -(2**63) - 1}]}}
    with pytest.raises(cp.CheckpointSerializationError) as excinfo:
        serde.dumps_typed(value)
    assert "['flow_state']['result'][1]['total']" in str(excinfo.value)


def test_guard_passes_through_in_range_values():
    serde = _guarded_memory_serde()
    type_, _ = serde.dumps_typed({"n": 7, "edge_low": -(2**63), "edge_high": 2**64 - 1})
    assert type_ == "msgpack"


def test_guard_names_the_true_msgpack_culprit_not_the_uint64():
    # A payload carrying BOTH a valid uint64 (in [2**63, 2**64-1], which msgpack
    # encodes fine) and a > 2**64 value (the real encode failure) must name the
    # > 2**64 value — the reactive guard fires on msgpack's actual abort, so it
    # names the true culprit rather than the in-range uint64.
    serde = _guarded_memory_serde()
    value = {"uint": 2**64 - 1, "huge": 2**64}
    with pytest.raises(cp.CheckpointSerializationError) as excinfo:
        serde.dumps_typed(value)
    message = str(excinfo.value)
    assert "['huge']" in message
    assert str(2**64) in message
    assert "['uint']" not in message


def test_guard_keeps_valid_uint64_values():
    # A tool payload may hold a valid uint64; a payload with only an
    # in-[2**63, 2**64-1] value encodes fine and does NOT raise at checkpoint.
    serde = _guarded_memory_serde()
    type_, _ = serde.dumps_typed({"result": {"total": 2**63}, "max": 2**64 - 1})
    assert type_ == "msgpack"


def test_guarded_saver_write_raises_typed_error_naming_path():
    # A REAL checkpoint WRITE through the guarded saver (obtained via
    # get_saver_from_resource, not calling serde.dumps_typed directly) triggers the
    # guard for a > 2**64 channel value — proving the guard is wired into the saver
    # the graph checkpoints through, not just the serde in isolation.
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.memory import InMemorySaver

    saver = cp.get_saver_from_resource("memory", _resource("memory", InMemorySaver()))
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"payload": {"count": 2**64}}
    config: Any = {"configurable": {"thread_id": "t1", "checkpoint_ns": ""}}
    with pytest.raises(cp.CheckpointSerializationError) as excinfo:
        saver.put(config, checkpoint, {}, {"payload": 1})
    message = str(excinfo.value)
    assert "['count']" in message
    assert str(2**64) in message


def test_guard_non_integer_encode_failure_still_raises_typed_error():
    # A value msgpack cannot encode for a reason other than integer overflow still
    # becomes the loud typed error — never a silent pickle fallback.
    serde = _guarded_memory_serde()
    with pytest.raises(cp.CheckpointSerializationError, match="could not be msgpack-encoded"):
        serde.dumps_typed({"handle": object()})


def test_guard_is_idempotent():
    from langgraph.checkpoint.memory import InMemorySaver

    saver = cp.get_saver_from_resource("memory", _resource("memory", InMemorySaver()))
    once = saver.serde
    twice = cp.get_saver_from_resource("memory", _resource("memory", saver)).serde
    assert once is twice
    assert isinstance(twice, cp._GuardedSerializer)
