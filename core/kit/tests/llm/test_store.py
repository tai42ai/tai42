"""Store factory + registry: provider selection, the index-exclusion cache key,
the benign-setup-race guard, and close-all error collection. Backends mocked at
their import seam — no real Redis/Postgres/SQLite connection is opened.
"""

import asyncio
import sys
import types
from typing import Any

import pytest

pytest.importorskip("langgraph")

from tai42_kit.llm.store import store as st
from tai42_kit.llm.store.store_registry import StoreRegistry


# --------------------------------------------------------------------------- #
# create_store_resource — provider branches
# --------------------------------------------------------------------------- #
async def test_memory_resource_returns_store_and_noop_close():
    from langgraph.store.memory import InMemoryStore

    resource, closer = await st.create_store_resource("memory")
    assert isinstance(resource, InMemoryStore)
    await closer()


async def test_memory_drops_none_kwargs(monkeypatch):
    captured = {}

    class _FakeStore:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(st, "InMemoryStore", _FakeStore)
    await st.create_store_resource("memory", index=None, ttl={"x": 1})
    # None-valued kwargs are filtered out; real ones flow through.
    assert "index" not in captured
    assert captured["ttl"] == {"x": 1}


async def test_unsupported_provider_raises():
    with pytest.raises(ValueError, match="Unsupported store provider"):
        await st.create_store_resource("bogus")


async def test_sqlite_requires_conn_string():
    with pytest.raises(ValueError, match="sqlite store provider requires"):
        await st.create_store_resource("sqlite", None)


async def test_postgres_requires_conn_string():
    with pytest.raises(ValueError, match="postgres store provider requires"):
        await st.create_store_resource("postgres", None)


async def test_redis_requires_conn_string(monkeypatch):
    # A fake module without AsyncRedisStore would make the langgraph redis
    # import fail, so reaching the ValueError proves the conn_string guard
    # fires before the import.
    fake_mod: Any = types.ModuleType("langgraph.store.redis")
    monkeypatch.setitem(sys.modules, "langgraph.store.redis", fake_mod)
    with pytest.raises(ValueError, match="redis store provider requires"):
        await st.create_store_resource("redis", None)


async def test_redis_resource_setup_called(monkeypatch):
    setups = []

    class _FakeStore:
        def __init__(self, redis_url, **kwargs):
            self.redis_url = redis_url
            self.closed = False

        async def setup(self):
            setups.append(self.redis_url)

        async def __aexit__(self, *exc):
            self.closed = True

    fake_mod: Any = types.ModuleType("langgraph.store.redis")
    fake_mod.AsyncRedisStore = _FakeStore
    monkeypatch.setitem(sys.modules, "langgraph.store.redis", fake_mod)

    resource, closer = await st.create_store_resource("redis", "redis://h/0")
    assert isinstance(resource, _FakeStore)
    assert setups == ["redis://h/0"]
    await closer()
    # The closer tears the store down (disconnects the redis pool), not a no-op.
    assert resource.closed is True


async def test_redis_setup_ignores_index_already_exists(monkeypatch):
    class _FakeStore:
        def __init__(self, redis_url, **kwargs):
            pass

        async def setup(self):
            raise RuntimeError("Index already exists")

    fake_mod: Any = types.ModuleType("langgraph.store.redis")
    fake_mod.AsyncRedisStore = _FakeStore
    monkeypatch.setitem(sys.modules, "langgraph.store.redis", fake_mod)

    resource, _ = await st.create_store_resource("redis", "redis://h/0")
    assert isinstance(resource, _FakeStore)


async def test_redis_setup_reraises_other_errors(monkeypatch):
    closed = []

    class _FakeStore:
        def __init__(self, redis_url, **kwargs):
            pass

        async def setup(self):
            raise RuntimeError("boom")

        async def __aexit__(self, *exc):
            closed.append(True)

    fake_mod: Any = types.ModuleType("langgraph.store.redis")
    fake_mod.AsyncRedisStore = _FakeStore
    monkeypatch.setitem(sys.modules, "langgraph.store.redis", fake_mod)

    with pytest.raises(RuntimeError, match="boom"):
        await st.create_store_resource("redis", "redis://h/0")
    # A non-benign setup failure tears down the store we opened, not a leak.
    assert closed == [True]


async def test_sqlite_resource_builds_and_closes(monkeypatch):
    closed = []

    class _FakeConn:
        async def close(self):
            closed.append(True)

    class _FakeStore:
        def __init__(self, conn, **kwargs):
            pass

        async def setup(self):
            pass

    aiosqlite_mod: Any = types.ModuleType("aiosqlite")

    async def _connect(path):
        return _FakeConn()

    aiosqlite_mod.connect = _connect
    store_mod: Any = types.ModuleType("langgraph.store.sqlite")
    store_mod.AsyncSqliteStore = _FakeStore
    monkeypatch.setitem(sys.modules, "aiosqlite", aiosqlite_mod)
    monkeypatch.setitem(sys.modules, "langgraph.store.sqlite", store_mod)

    resource, closer = await st.create_store_resource("sqlite", "/tmp/x.db")
    # The sqlite store branch returns the store object (the conn is closed via closer).
    assert isinstance(resource, _FakeStore)
    await closer()
    assert closed == [True]


async def test_postgres_resource_builds_and_closes(monkeypatch):
    closed = []

    class _FakeConnCtx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    class _FakePool:
        def __init__(self, conn_string, **kwargs):
            self.conn_string = conn_string

        async def open(self):
            pass

        def connection(self):
            return _FakeConnCtx()

        async def close(self):
            closed.append(True)

    class _FakeStore:
        def __init__(self, conn, **kwargs):
            pass

        async def setup(self):
            pass

    pool_mod: Any = types.ModuleType("psycopg_pool")
    pool_mod.AsyncConnectionPool = _FakePool
    store_mod: Any = types.ModuleType("langgraph.store.postgres")
    store_mod.AsyncPostgresStore = _FakeStore
    rows_mod: Any = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_mod)
    monkeypatch.setitem(sys.modules, "langgraph.store.postgres", store_mod)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_mod)

    resource, closer = await st.create_store_resource("postgres", "postgresql://u@h/db")
    assert isinstance(resource, _FakePool)
    await closer()
    assert closed == [True]


async def test_sqlite_resource_closes_conn_on_setup_failure(monkeypatch):
    # setup() failing must not leak the connection opened just before it: no
    # cleanup fn is returned on this path, so the branch closes it itself.
    closed = []

    class _FakeConn:
        async def close(self):
            closed.append(True)

    class _FakeStore:
        def __init__(self, conn, **kwargs):
            pass

        async def setup(self):
            raise RuntimeError("setup boom")

    aiosqlite_mod: Any = types.ModuleType("aiosqlite")

    async def _connect(path):
        return _FakeConn()

    aiosqlite_mod.connect = _connect
    store_mod: Any = types.ModuleType("langgraph.store.sqlite")
    store_mod.AsyncSqliteStore = _FakeStore
    monkeypatch.setitem(sys.modules, "aiosqlite", aiosqlite_mod)
    monkeypatch.setitem(sys.modules, "langgraph.store.sqlite", store_mod)

    with pytest.raises(RuntimeError, match="setup boom"):
        await st.create_store_resource("sqlite", "/tmp/x.db")
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

    class _FakeStore:
        def __init__(self, conn, **kwargs):
            pass

        async def setup(self):
            raise RuntimeError("setup boom")

    pool_mod: Any = types.ModuleType("psycopg_pool")
    pool_mod.AsyncConnectionPool = _FakePool
    store_mod: Any = types.ModuleType("langgraph.store.postgres")
    store_mod.AsyncPostgresStore = _FakeStore
    rows_mod: Any = types.ModuleType("psycopg.rows")
    rows_mod.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_mod)
    monkeypatch.setitem(sys.modules, "langgraph.store.postgres", store_mod)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows_mod)

    with pytest.raises(RuntimeError, match="setup boom"):
        await st.create_store_resource("postgres", "postgresql://u@h/db")
    assert closed == [True]


# --------------------------------------------------------------------------- #
# get_store_from_resource
# --------------------------------------------------------------------------- #
def test_get_store_passthrough_providers():
    sentinel = object()
    for provider in ("memory", "sqlite", "redis"):
        assert st.get_store_from_resource(provider, sentinel) is sentinel


def test_get_store_postgres_wraps_with_kwargs(monkeypatch):
    captured = {}

    class _FakeStore:
        def __init__(self, resource, **kwargs):
            captured["resource"] = resource
            captured.update(kwargs)

    store_mod: Any = types.ModuleType("langgraph.store.postgres")
    store_mod.AsyncPostgresStore = _FakeStore
    monkeypatch.setitem(sys.modules, "langgraph.store.postgres", store_mod)
    out = st.get_store_from_resource("postgres", "pool", ttl=5)
    assert isinstance(out, _FakeStore)
    assert captured == {"resource": "pool", "ttl": 5}


def test_get_store_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown provider"):
        st.get_store_from_resource("bogus", object())


# --------------------------------------------------------------------------- #
# StoreRegistry
# --------------------------------------------------------------------------- #
def test_key_index_handling_per_provider():
    reg = StoreRegistry()
    # postgres: the cached resource is an index-agnostic pool — 'index' is
    # excluded, so two calls differing only by index share one pool.
    p1 = reg._generate_key("postgres", "conn", {"index": object(), "ttl": 5})
    p2 = reg._generate_key("postgres", "conn", {"index": "other", "ttl": 5})
    assert p1 == p2
    # redis (and sqlite/memory): the cached resource IS the store with 'index'
    # baked in — differing index configs must NOT share a key.
    r1 = reg._generate_key("redis", "conn", {"index": {"dims": 128}, "ttl": 5})
    r2 = reg._generate_key("redis", "conn", {"index": {"dims": 256}, "ttl": 5})
    assert r1 != r2
    # A non-serializable index value is fingerprinted, not a crash.
    reg._generate_key("redis", "conn", {"index": object(), "ttl": 5})
    # A differing serializable kwarg DOES split the key.
    k3 = reg._generate_key("redis", "conn", {"ttl": 9})
    assert k3 != r1


async def test_registry_caches_resource_per_key(monkeypatch):
    creates = []

    async def _fake_create(provider, conn_string, **kwargs):
        creates.append((provider, conn_string))
        return (f"res-{conn_string}", lambda: None)

    monkeypatch.setattr("tai42_kit.llm.store.store_registry.create_store_resource", _fake_create)
    monkeypatch.setattr(
        "tai42_kit.llm.store.store_registry.get_store_from_resource",
        lambda provider, resource, **kw: resource,
    )

    reg = StoreRegistry()
    a = await reg.get_store("memory", "c1")
    b = await reg.get_store("memory", "c1")
    assert a == b == "res-c1"
    assert creates == [("memory", "c1")]  # built once, then cached


async def test_registry_close_all_collects_errors():
    reg = StoreRegistry()

    async def _ok():
        pass

    async def _boom():
        raise RuntimeError("close failed")

    reg._resources = {"k1": (object(), _ok), "k2": (object(), _boom)}
    reg._locks = {"k1": asyncio.Lock(), "k2": asyncio.Lock()}
    with pytest.raises(ExceptionGroup) as ei:
        await reg.close_all()
    assert len(ei.value.exceptions) == 1
    assert reg._resources == {}
    assert reg._locks == {}


async def test_registry_close_all_clean():
    reg = StoreRegistry()

    async def _ok():
        pass

    reg._resources = {"k1": (object(), _ok)}
    await reg.close_all()
    assert reg._resources == {}


async def test_registry_close_all_skips_falsy_closer():
    reg = StoreRegistry()
    reg._resources = {"k1": (object(), None)}
    await reg.close_all()
    assert reg._resources == {}


async def test_get_after_close_closes_new_resource_and_raises(monkeypatch):
    # A get that finishes creating its resource after close_all() must not
    # register (leak) it: it closes the freshly-opened resource and fails loudly.
    closed = []

    async def _closer():
        closed.append(True)

    async def _fake_create(provider, conn_string, **kwargs):
        return (object(), _closer)

    monkeypatch.setattr("tai42_kit.llm.store.store_registry.create_store_resource", _fake_create)

    reg = StoreRegistry()
    await reg.close_all()
    with pytest.raises(RuntimeError, match="StoreRegistry is closed"):
        await reg.get_store("memory", "c1")
    assert closed == [True]
    assert reg._resources == {}


async def test_registry_concurrent_first_use_creates_once(monkeypatch):
    import asyncio

    creates = []

    async def _slow_create(provider, conn_string, **kwargs):
        creates.append(conn_string)
        await asyncio.sleep(0.01)  # hold the create lock so the sibling waits
        return ("res", lambda: None)

    monkeypatch.setattr("tai42_kit.llm.store.store_registry.create_store_resource", _slow_create)
    monkeypatch.setattr(
        "tai42_kit.llm.store.store_registry.get_store_from_resource",
        lambda provider, resource, **kw: resource,
    )

    reg = StoreRegistry()
    a, b = await asyncio.gather(reg.get_store("memory", "c"), reg.get_store("memory", "c"))
    # Second caller finds the cached resource after the lock -> created once.
    assert a == b == "res"
    assert creates == ["c"]


def test_store_registry_singleton_per_loop():
    from tai42_kit.llm.store.store_registry import store_registry

    async def _pair():
        return store_registry(), store_registry()

    a, b = asyncio.run(_pair())
    assert a is b  # one registry per loop
    c, d = asyncio.run(_pair())
    assert c is d
    assert c is not a  # a second loop gets its own registry


def test_store_registry_usable_across_event_loops(monkeypatch):
    # A registry created and used in one event loop must not poison later
    # loops: each asyncio.run gets its own registry, so no loop-bound
    # asyncio.Lock or resource is reused across loops.
    creates = []

    async def _fake_create(provider, conn_string, **kwargs):
        creates.append((provider, conn_string))
        return (f"res-{conn_string}", None)

    monkeypatch.setattr("tai42_kit.llm.store.store_registry.create_store_resource", _fake_create)
    monkeypatch.setattr(
        "tai42_kit.llm.store.store_registry.get_store_from_resource",
        lambda provider, resource, **kw: resource,
    )

    from tai42_kit.llm.store.store_registry import store_registry

    async def _use(conn):
        return await store_registry().get_store("memory", conn)

    assert asyncio.run(_use("c1")) == "res-c1"
    # The second loop must not trip over asyncio primitives bound to the first.
    assert asyncio.run(_use("c2")) == "res-c2"
    assert creates == [("memory", "c1"), ("memory", "c2")]


async def test_store_registry_accessor_rebuilds_after_close_all():
    from tai42_kit.llm.store.store_registry import store_registry

    reg = store_registry()
    await reg.close_all()
    # close_all() must not brick the accessor: the next call builds a fresh,
    # open registry instead of returning the closed one.
    fresh = store_registry()
    assert fresh is not reg
    assert fresh._closed is False


async def test_store_registry_settings_reset_drops_registry():
    from tai42_kit.llm.store.store_registry import store_registry
    from tai42_kit.settings import reset_all_settings

    reg = store_registry()
    reset_all_settings()
    # The reset hook drops the per-loop registries so a soft restart rebuilds
    # them with the fresh settings on next use.
    assert store_registry() is not reg


async def test_store_registry_settings_reset_raises_on_live_resource(monkeypatch):
    from tai42_kit.llm.store import store_registry as reg_mod
    from tai42_kit.settings import reset_all_settings

    async def _fake_create(provider, conn_string, **kwargs):
        async def _closer():
            pass

        return (object(), _closer)

    monkeypatch.setattr(reg_mod, "create_store_resource", _fake_create)
    monkeypatch.setattr(reg_mod, "get_store_from_resource", lambda provider, resource, **kw: resource)

    reg = reg_mod.store_registry()
    await reg.get_store("memory", "c1")  # the running-loop registry now holds a live resource
    assert reg.has_live_resources is True

    # Dropping it here would leak an open resource, so the reset must fail loudly
    # and demand an explicit close_all() first rather than silently clearing.
    with pytest.raises(RuntimeError, match="close_all"):
        reset_all_settings()

    # After the explicit close_all the reset succeeds and rebuilds on next use.
    await reg.close_all()
    reset_all_settings()
    assert reg_mod.store_registry() is not reg
