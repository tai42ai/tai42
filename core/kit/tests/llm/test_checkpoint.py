"""Checkpoint factory + registry: provider selection, the benign-setup-race
guard, and the resource cache/close-all error collection.

The memory backend is real (in-process, no I/O). The redis/sqlite/postgres
backends are mocked at their import seam — sqlite/postgres modules are injected
into sys.modules (their extras are not installed), redis attributes are patched
on the real module. No real Redis/Postgres/SQLite connection is opened.
"""

import asyncio
import sys
import types
from typing import Any

import pytest

pytest.importorskip("langgraph")

from tai42_kit.llm.checkpoint import checkpoint as cp
from tai42_kit.llm.checkpoint.checkpoint_registry import CheckpointRegistry


# --------------------------------------------------------------------------- #
# create_checkpoint_resource — provider branches
# --------------------------------------------------------------------------- #
async def test_memory_resource_returns_saver_and_noop_close():
    from langgraph.checkpoint.memory import InMemorySaver

    resource, closer = await cp.create_checkpoint_resource("memory")
    assert isinstance(resource, InMemorySaver)
    await closer()  # no-op, must not raise


async def test_unsupported_provider_raises():
    with pytest.raises(ValueError, match="Unsupported checkpoint provider"):
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
    # None + redis + a TAI_DEFAULT_REDIS_URL resolves the checkpoint end-to-end
    # from the shared namespace: the saver is built from that resolved URL.
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")
    setups = []

    class _FakeSaver:
        def __init__(self, redis_url, ttl=None):
            self.redis_url = redis_url

        async def asetup(self):
            setups.append(self.redis_url)

        async def __aexit__(self, *exc):
            pass

    fake_mod: Any = types.ModuleType("langgraph.checkpoint.redis")
    fake_mod.AsyncRedisSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.redis", fake_mod)

    resource, closer = await cp.create_checkpoint_resource("redis", None)
    assert resource.redis_url == "redis://shared:6379/0"
    assert setups == ["redis://shared:6379/0"]
    await closer()


async def test_postgres_none_conn_string_resolves_from_base_pg_settings(monkeypatch):
    # None + postgres resolves to the base Postgres DSN, so the pool is opened
    # against the identity from the base ``PG_*`` namespace.
    monkeypatch.setenv("PG_HOST", "shared-db")
    monkeypatch.setenv("PG_PASSWORD", "shared-secret")
    captured: dict[str, Any] = {}

    class _FakeConnCtx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    class _FakePool:
        def __init__(self, conn_string, **kwargs):
            captured["conn_string"] = conn_string

        async def open(self):
            pass

        def connection(self):
            return _FakeConnCtx()

        async def close(self):
            pass

    class _FakeSaver:
        def __init__(self, conn):
            pass

        async def setup(self):
            pass

    pool_mod: Any = types.ModuleType("psycopg_pool")
    pool_mod.AsyncConnectionPool = _FakePool
    saver_mod: Any = types.ModuleType("langgraph.checkpoint.postgres.aio")
    saver_mod.AsyncPostgresSaver = _FakeSaver
    rows_mod: Any = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_mod)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", saver_mod)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_mod)

    _resource, closer = await cp.create_checkpoint_resource("postgres", None)
    assert captured["conn_string"].startswith("postgresql://")
    assert "shared-db" in captured["conn_string"]
    await closer()


async def test_redis_resource_setup_called(monkeypatch):
    setups = []

    class _FakeSaver:
        def __init__(self, redis_url, ttl=None):
            self.redis_url = redis_url
            self.ttl = ttl
            self.closed = False

        async def asetup(self):
            setups.append(self.redis_url)

        async def __aexit__(self, *exc):
            self.closed = True

    fake_mod: Any = types.ModuleType("langgraph.checkpoint.redis")
    fake_mod.AsyncRedisSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.redis", fake_mod)

    resource, closer = await cp.create_checkpoint_resource("redis", "redis://h:6379/0")
    assert isinstance(resource, _FakeSaver)
    assert setups == ["redis://h:6379/0"]
    await closer()
    # The closer tears the saver down (disconnects the redis pool), not a no-op.
    assert resource.closed is True


async def test_redis_setup_ignores_already_exists(monkeypatch):
    class _FakeSaver:
        def __init__(self, redis_url, ttl=None):
            pass

        async def asetup(self):
            raise RuntimeError("Index already exists")

    fake_mod: Any = types.ModuleType("langgraph.checkpoint.redis")
    fake_mod.AsyncRedisSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.redis", fake_mod)

    resource, _ = await cp.create_checkpoint_resource("redis", "redis://h/0")
    assert isinstance(resource, _FakeSaver)


async def test_redis_setup_reraises_other_errors(monkeypatch):
    closed = []

    class _FakeSaver:
        def __init__(self, redis_url, ttl=None):
            pass

        async def asetup(self):
            raise RuntimeError("connection refused")

        async def __aexit__(self, *exc):
            closed.append(True)

    fake_mod: Any = types.ModuleType("langgraph.checkpoint.redis")
    fake_mod.AsyncRedisSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.redis", fake_mod)

    with pytest.raises(RuntimeError, match="connection refused"):
        await cp.create_checkpoint_resource("redis", "redis://h/0")
    # A non-benign setup failure tears down the saver we opened, not a leak.
    assert closed == [True]


def _install_fake_redis_saver(monkeypatch) -> dict[str, Any]:
    """Install a fake ``AsyncRedisSaver`` that records the ``ttl`` it was built with."""
    captured: dict[str, Any] = {}

    class _FakeSaver:
        def __init__(self, redis_url, ttl=None):
            captured["ttl"] = ttl

        async def asetup(self):
            pass

        async def __aexit__(self, *exc):
            pass

    fake_mod: Any = types.ModuleType("langgraph.checkpoint.redis")
    fake_mod.AsyncRedisSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.redis", fake_mod)
    return captured


async def test_redis_ttl_unset_passes_no_ttl(monkeypatch):
    # With the setting unset the saver gets no TTL, so redis keeps checkpoints
    # forever — today's behavior.
    from types import SimpleNamespace

    captured = _install_fake_redis_saver(monkeypatch)
    monkeypatch.setattr(
        "tai42_kit.llm.settings.llm_provider_settings",
        lambda: SimpleNamespace(checkpoint_ttl_minutes=None),
    )
    await cp.create_checkpoint_resource("redis", "redis://h/0")
    assert captured["ttl"] is None


async def test_redis_ttl_set_passes_idle_ttl_config(monkeypatch):
    # A set TTL becomes the redis key TTL with ``refresh_on_read`` enabled, so the
    # lifetime measures IDLE time (reads and writes both restart the countdown).
    from types import SimpleNamespace

    captured = _install_fake_redis_saver(monkeypatch)
    monkeypatch.setattr(
        "tai42_kit.llm.settings.llm_provider_settings",
        lambda: SimpleNamespace(checkpoint_ttl_minutes=120),
    )
    await cp.create_checkpoint_resource("redis", "redis://h/0")
    assert captured["ttl"] == {"default_ttl": 120, "refresh_on_read": True}


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
    assert isinstance(resource, _FakeConn)
    await closer()
    assert closed == [True]


async def test_postgres_resource_builds_and_closes(monkeypatch):
    closed = []
    captured_pool_kwargs = {}

    class _FakeConnCtx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    class _FakePool:
        def __init__(self, conn_string, **kwargs):
            self.conn_string = conn_string
            captured_pool_kwargs.update(kwargs)

        async def open(self):
            pass

        def connection(self):
            return _FakeConnCtx()

        async def close(self):
            closed.append(True)

    class _FakeSaver:
        def __init__(self, conn):
            pass

        async def setup(self):
            pass

    pool_mod: Any = types.ModuleType("psycopg_pool")
    pool_mod.AsyncConnectionPool = _FakePool
    saver_mod: Any = types.ModuleType("langgraph.checkpoint.postgres.aio")
    saver_mod.AsyncPostgresSaver = _FakeSaver
    rows_mod: Any = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_mod)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", saver_mod)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_mod)

    resource, closer = await cp.create_checkpoint_resource("postgres", "postgresql://u@h/db")
    assert isinstance(resource, _FakePool)
    # The saver's required connection kwargs are handed to the pool.
    assert captured_pool_kwargs["kwargs"] == {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": rows_mod.dict_row,
    }
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
    # open()+setup() failing must not leak the opened pool: no cleanup fn is
    # returned on this path, so the branch closes the pool itself.
    closed = []

    class _FakeConnCtx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    class _FakePool:
        def __init__(self, conn_string, **kwargs):
            pass

        async def open(self):
            pass

        def connection(self):
            return _FakeConnCtx()

        async def close(self):
            closed.append(True)

    class _FakeSaver:
        def __init__(self, conn):
            pass

        async def setup(self):
            raise RuntimeError("setup boom")

    pool_mod: Any = types.ModuleType("psycopg_pool")
    pool_mod.AsyncConnectionPool = _FakePool
    saver_mod: Any = types.ModuleType("langgraph.checkpoint.postgres.aio")
    saver_mod.AsyncPostgresSaver = _FakeSaver
    rows_mod: Any = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_mod)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", saver_mod)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_mod)

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


def test_get_saver_memory_and_redis_return_resource():
    # The same resource object is returned (its serde is guarded in place), so the
    # registry's cached resource identity is preserved.
    memory_saver = _FakeSaver()
    redis_saver = _FakeSaver()
    assert cp.get_saver_from_resource("memory", memory_saver) is memory_saver
    assert cp.get_saver_from_resource("redis", redis_saver) is redis_saver
    assert isinstance(memory_saver.serde, cp._GuardedSerializer)
    assert isinstance(redis_saver.serde, cp._GuardedSerializer)


def test_get_saver_sqlite_wraps_resource(monkeypatch):
    saver_mod: Any = types.ModuleType("langgraph.checkpoint.sqlite.aio")
    saver_mod.AsyncSqliteSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite.aio", saver_mod)
    out = cp.get_saver_from_resource("sqlite", "conn")
    assert isinstance(out, _FakeSaver)
    assert out.resource == "conn"
    # The saver the graph checkpoints through has its serializer guarded.
    assert isinstance(out.serde, cp._GuardedSerializer)


def test_get_saver_postgres_wraps_resource(monkeypatch):
    saver_mod: Any = types.ModuleType("langgraph.checkpoint.postgres.aio")
    saver_mod.AsyncPostgresSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", saver_mod)
    out = cp.get_saver_from_resource("postgres", "pool")
    assert isinstance(out, _FakeSaver)
    assert out.resource == "pool"
    assert isinstance(out.serde, cp._GuardedSerializer)


def test_get_saver_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown provider"):
        cp.get_saver_from_resource("bogus", object())


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

    return cp.get_saver_from_resource("memory", InMemorySaver()).serde


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


def test_guard_keeps_valid_uint64_from_doors_5_and_6():
    # Doors 5-6 carry arbitrary tool payloads that may hold a valid uint64; a
    # payload with only an in-[2**63, 2**64-1] value encodes fine and does NOT
    # raise at checkpoint.
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

    saver = cp.get_saver_from_resource("memory", InMemorySaver())
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

    saver = cp.get_saver_from_resource("memory", InMemorySaver())
    once = saver.serde
    twice = cp.get_saver_from_resource("memory", saver).serde
    assert once is twice
    assert isinstance(twice, cp._GuardedSerializer)


# --------------------------------------------------------------------------- #
# CheckpointRegistry
# --------------------------------------------------------------------------- #
async def test_registry_caches_resource_per_key(monkeypatch):
    creates = []

    async def _fake_create(provider, conn_string):
        creates.append((provider, conn_string))
        return (f"res-{conn_string}", lambda: None)

    monkeypatch.setattr("tai42_kit.llm.checkpoint.checkpoint_registry.create_checkpoint_resource", _fake_create)
    monkeypatch.setattr(
        "tai42_kit.llm.checkpoint.checkpoint_registry.get_saver_from_resource",
        lambda provider, resource: resource,
    )

    reg = CheckpointRegistry()
    a = await reg.get_checkpointer("memory", "c1")
    b = await reg.get_checkpointer("memory", "c1")
    c = await reg.get_checkpointer("memory", "c2")
    assert a == b == "res-c1"
    assert c == "res-c2"
    # c1 built once (cached on second call); c2 separate.
    assert creates == [("memory", "c1"), ("memory", "c2")]


async def test_registry_close_all_collects_errors():
    reg = CheckpointRegistry()

    async def _ok():
        pass

    async def _boom():
        raise RuntimeError("close failed")

    reg._resources = {"k1": (object(), _ok), "k2": (object(), _boom)}
    reg._locks = {"k1": asyncio.Lock(), "k2": asyncio.Lock()}
    with pytest.raises(ExceptionGroup) as ei:
        await reg.close_all()
    assert len(ei.value.exceptions) == 1
    # The registry is cleared even though one close failed.
    assert reg._resources == {}
    assert reg._locks == {}


async def test_registry_close_all_clean():
    reg = CheckpointRegistry()

    async def _ok():
        pass

    reg._resources = {"k1": (object(), _ok)}
    await reg.close_all()
    assert reg._resources == {}


async def test_registry_close_all_skips_falsy_closer():
    reg = CheckpointRegistry()
    # A resource registered without a closer is skipped, not called.
    reg._resources = {"k1": (object(), None)}
    await reg.close_all()
    assert reg._resources == {}


async def test_get_after_close_closes_new_resource_and_raises(monkeypatch):
    # A get that finishes creating its resource after close_all() must not
    # register (leak) it: it closes the freshly-opened resource and fails loudly.
    closed = []

    async def _closer():
        closed.append(True)

    async def _fake_create(provider, conn_string):
        return (object(), _closer)

    monkeypatch.setattr("tai42_kit.llm.checkpoint.checkpoint_registry.create_checkpoint_resource", _fake_create)

    reg = CheckpointRegistry()
    await reg.close_all()
    with pytest.raises(RuntimeError, match="CheckpointRegistry is closed"):
        await reg.get_checkpointer("memory", "c1")
    assert closed == [True]
    assert reg._resources == {}


async def test_registry_concurrent_first_use_creates_once(monkeypatch):
    import asyncio

    creates = []

    async def _slow_create(provider, conn_string):
        creates.append(conn_string)
        await asyncio.sleep(0.01)  # hold the create lock so the sibling waits
        return ("res", lambda: None)

    monkeypatch.setattr("tai42_kit.llm.checkpoint.checkpoint_registry.create_checkpoint_resource", _slow_create)
    monkeypatch.setattr(
        "tai42_kit.llm.checkpoint.checkpoint_registry.get_saver_from_resource",
        lambda provider, resource: resource,
    )

    reg = CheckpointRegistry()
    a, b = await asyncio.gather(reg.get_checkpointer("memory", "c"), reg.get_checkpointer("memory", "c"))
    # The second caller wakes after the lock and sees the cached resource — the
    # double-checked guard prevents a second create.
    assert a == b == "res"
    assert creates == ["c"]


def test_checkpoint_registry_singleton_per_loop():
    from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry

    async def _pair():
        return checkpoint_registry(), checkpoint_registry()

    a, b = asyncio.run(_pair())
    assert a is b  # one registry per loop
    c, d = asyncio.run(_pair())
    assert c is d
    assert c is not a  # a second loop gets its own registry


def test_checkpoint_registry_usable_across_event_loops(monkeypatch):
    # A registry created and used in one event loop must not poison later
    # loops: each asyncio.run gets its own registry, so no loop-bound
    # asyncio.Lock or resource is reused across loops.
    creates = []

    async def _fake_create(provider, conn_string):
        creates.append((provider, conn_string))
        return (f"res-{conn_string}", None)

    monkeypatch.setattr("tai42_kit.llm.checkpoint.checkpoint_registry.create_checkpoint_resource", _fake_create)
    monkeypatch.setattr(
        "tai42_kit.llm.checkpoint.checkpoint_registry.get_saver_from_resource",
        lambda provider, resource: resource,
    )

    from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry

    async def _use(conn):
        return await checkpoint_registry().get_checkpointer("memory", conn)

    assert asyncio.run(_use("c1")) == "res-c1"
    # The second loop must not trip over asyncio primitives bound to the first.
    assert asyncio.run(_use("c2")) == "res-c2"
    assert creates == [("memory", "c1"), ("memory", "c2")]


async def test_checkpoint_registry_accessor_rebuilds_after_close_all():
    from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry

    reg = checkpoint_registry()
    await reg.close_all()
    # close_all() must not brick the accessor: the next call builds a fresh,
    # open registry instead of returning the closed one.
    fresh = checkpoint_registry()
    assert fresh is not reg
    assert fresh._closed is False


async def test_checkpoint_registry_settings_reset_drops_registry():
    from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
    from tai42_kit.settings import reset_all_settings

    reg = checkpoint_registry()
    reset_all_settings()
    # The reset hook drops the per-loop registries so a soft restart rebuilds
    # them with the fresh settings on next use.
    assert checkpoint_registry() is not reg


async def test_checkpoint_registry_settings_reset_raises_on_live_resource(monkeypatch):
    from tai42_kit.llm.checkpoint import checkpoint_registry as reg_mod
    from tai42_kit.settings import reset_all_settings

    async def _fake_create(provider, conn_string):
        async def _closer():
            pass

        return (object(), _closer)

    monkeypatch.setattr(reg_mod, "create_checkpoint_resource", _fake_create)
    monkeypatch.setattr(reg_mod, "get_saver_from_resource", lambda provider, resource: resource)

    reg = reg_mod.checkpoint_registry()
    await reg.get_checkpointer("memory", "c1")  # the running-loop registry now holds a live resource
    assert reg.has_live_resources is True

    # Dropping it here would leak an open resource, so the reset must fail loudly
    # and demand an explicit close_all() first rather than silently clearing.
    with pytest.raises(RuntimeError, match="close_all"):
        reset_all_settings()

    # After the explicit close_all the reset succeeds and rebuilds on next use.
    await reg.close_all()
    reset_all_settings()
    assert reg_mod.checkpoint_registry() is not reg
