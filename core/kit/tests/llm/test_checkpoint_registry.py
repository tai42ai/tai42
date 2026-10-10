"""CheckpointRegistry: per-key resource caching, close_all error collection,
singleton-per-loop, usable-across-loops, and the settings-reset drop hook.
"""

import asyncio

import pytest

pytest.importorskip("langgraph")

from tai42_kit.llm.checkpoint.checkpoint_registry import CheckpointRegistry


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


# --------------------------------------------------------------------------- #
# A declared retention: a saver view per waiting value on a native-TTL store
# --------------------------------------------------------------------------- #
class _FakeResource:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.handle = object()


@pytest.fixture
def views(monkeypatch):
    from tai42_kit.llm.checkpoint import checkpoint_registry as reg_mod
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES", "600")
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_FINISHED_MINUTES", "120")
    reset_all_settings()
    built: list[tuple[object, int]] = []

    async def _fake_create(provider, conn_string):
        return (_FakeResource(provider), None)

    async def _fake_view(resource, waiting_minutes):
        built.append((resource, waiting_minutes))
        return ("view", resource, waiting_minutes)

    monkeypatch.setattr(reg_mod, "create_checkpoint_resource", _fake_create)
    monkeypatch.setattr(reg_mod, "create_redis_saver_view", _fake_view)
    monkeypatch.setattr(reg_mod, "get_saver_from_resource", lambda provider, resource: ("handle", resource))
    yield built
    reset_all_settings()


async def test_a_declared_waiting_on_redis_gets_a_view_with_that_waiting(views):
    from tai42_kit.llm.checkpoint import ThreadRetention

    reg = CheckpointRegistry()
    view = await reg.get_checkpointer("redis", "c", ThreadRetention(2, 1))
    resource = await reg.resource("redis", "c")
    assert view == ("view", resource, 2)


async def test_the_same_waiting_twice_is_the_same_view_built_once(views):
    from tai42_kit.llm.checkpoint import ThreadRetention

    reg = CheckpointRegistry()
    a = await reg.get_checkpointer("redis", "c", ThreadRetention(2, 1))
    b = await reg.get_checkpointer("redis", "c", ThreadRetention(2, 2))
    c = await reg.get_checkpointer("redis", "c", ThreadRetention(5, 1))
    assert a is b
    assert c is not a
    assert [waiting for _resource, waiting in views] == [2, 5]


async def test_no_retention_and_the_platform_waiting_are_the_resource_handle(views):
    from tai42_kit.llm.checkpoint import ThreadRetention

    reg = CheckpointRegistry()
    resource = await reg.resource("redis", "c")
    assert await reg.get_checkpointer("redis", "c") == ("handle", resource)
    assert await reg.get_checkpointer("redis", "c", None) == ("handle", resource)
    assert await reg.get_checkpointer("redis", "c", ThreadRetention(600, 30)) == ("handle", resource)
    assert views == []


@pytest.mark.parametrize("provider", ["postgres", "sqlite", "memory"])
async def test_a_store_without_native_ttl_returns_the_same_saver_for_any_retention(views, provider):
    from tai42_kit.llm.checkpoint import ThreadRetention

    reg = CheckpointRegistry()
    resource = await reg.resource(provider, "c")
    assert await reg.get_checkpointer(provider, "c", ThreadRetention(2, 1)) == ("handle", resource)
    assert views == []


async def test_the_views_go_with_close_all(views):
    from tai42_kit.llm.checkpoint import ThreadRetention

    reg = CheckpointRegistry()
    await reg.get_checkpointer("redis", "c", ThreadRetention(2, 1))
    assert reg.has_live_resources is True
    await reg.close_all()
    assert reg.has_live_resources is False


async def test_a_saver_view_needs_a_redis_resource():
    from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource, create_redis_saver_view
    from tai42_kit.llm.checkpoint.ledger import MemoryFinishedThreadLedger

    resource = CheckpointResource("memory", object(), MemoryFinishedThreadLedger())
    with pytest.raises(ValueError, match="a Redis saver view needs a redis checkpoint resource, got 'memory'"):
        await create_redis_saver_view(resource, 2)
