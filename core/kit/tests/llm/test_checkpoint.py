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


async def test_postgres_requires_conn_string():
    with pytest.raises(ValueError, match="postgres checkpoint provider requires"):
        await cp.create_checkpoint_resource("postgres", None)


async def test_redis_requires_conn_string(monkeypatch):
    # A fake module without AsyncRedisSaver would make the langgraph redis
    # import fail, so reaching the ValueError proves the conn_string guard
    # fires before the import.
    fake_mod: Any = types.ModuleType("langgraph.checkpoint.redis")
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.redis", fake_mod)
    with pytest.raises(ValueError, match="redis checkpoint provider requires"):
        await cp.create_checkpoint_resource("redis", None)


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
def test_get_saver_memory_and_redis_return_resource():
    sentinel = object()
    assert cp.get_saver_from_resource("memory", sentinel) is sentinel
    assert cp.get_saver_from_resource("redis", sentinel) is sentinel


def test_get_saver_sqlite_wraps_resource(monkeypatch):
    class _FakeSaver:
        def __init__(self, resource):
            self.resource = resource

    saver_mod: Any = types.ModuleType("langgraph.checkpoint.sqlite.aio")
    saver_mod.AsyncSqliteSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite.aio", saver_mod)
    out = cp.get_saver_from_resource("sqlite", "conn")
    assert isinstance(out, _FakeSaver)
    assert out.resource == "conn"


def test_get_saver_postgres_wraps_resource(monkeypatch):
    class _FakeSaver:
        def __init__(self, resource):
            self.resource = resource

    saver_mod: Any = types.ModuleType("langgraph.checkpoint.postgres.aio")
    saver_mod.AsyncPostgresSaver = _FakeSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", saver_mod)
    out = cp.get_saver_from_resource("postgres", "pool")
    assert isinstance(out, _FakeSaver)
    assert out.resource == "pool"


def test_get_saver_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown provider"):
        cp.get_saver_from_resource("bogus", object())


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
