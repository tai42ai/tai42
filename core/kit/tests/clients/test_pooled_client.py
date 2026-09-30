"""PooledClient pooling + the client_ctx facade — no external driver needed."""

import asyncio

import pytest

from tai42_kit.clients import ClientSettings, PooledClient, client_ctx, shutdown_all_clients


def _make_client_cls():
    """A fresh PooledClient subclass per test so the module-global pool (keyed by
    class) never leaks counts across tests."""

    class _Conn:
        def __init__(self, kwargs):
            self.kwargs = kwargs
            self.closed = False

    class _FakeClient(PooledClient):
        created = 0
        closed = 0

        async def _create(self, **kwargs):
            type(self).created += 1
            return _Conn(kwargs)

        async def _close(self, client):
            type(self).closed += 1
            client.closed = True

    return _FakeClient


async def test_current_pools_by_key():
    cls = _make_client_cls()
    async with cls().current(url="a") as c1:
        async with cls().current(url="a") as c2:
            assert c1 is c2  # same key -> one pooled connection
        async with cls().current(url="b") as c3:
            assert c3 is not c1  # different key -> separate connection
    assert cls.created == 2


def test_current_pools_by_key_per_loop():
    # The loop-keyed isolation invariant: the same class + key resolves to one
    # shared connection WITHIN a loop, but each event loop gets its own pooled
    # connection (async pools bind to the loop that created them).
    cls = _make_client_cls()
    seen = []

    async def _use():
        async with cls().current(url="a") as c1:
            async with cls().current(url="a") as c2:
                assert c1 is c2  # same loop + same key -> one shared connection
            seen.append(c1)

    asyncio.run(_use())
    asyncio.run(_use())

    # Two separate loops -> two distinct pooled connections for the same key.
    assert len(seen) == 2
    assert seen[0] is not seen[1]
    assert cls.created == 2


async def test_close_drops_pooled_connection():
    cls = _make_client_cls()
    async with cls().current(url="a"):
        pass
    await cls().close(url="a")
    assert cls.closed == 1
    # next use rebuilds
    async with cls().current(url="a"):
        pass
    assert cls.created == 2


async def test_fresh_path_builds_and_closes_off_pool():
    cls = _make_client_cls()
    async with client_ctx(cls, fresh=True, url="a") as conn:
        assert conn.closed is False
    assert cls.created == 1
    assert cls.closed == 1


async def test_client_ctx_uses_settings_kwargs():
    cls = _make_client_cls()

    class _Settings(ClientSettings):
        def client_kwargs(self):
            return {"url": "from-settings"}

    async with client_ctx(cls, _Settings()) as conn:
        assert conn.kwargs == {"url": "from-settings"}


async def test_shutdown_all_clients_closes_live_pools():
    cls = _make_client_cls()
    async with cls().current(url="a"):
        pass
    async with cls().current(url="b"):
        pass
    await shutdown_all_clients()
    assert cls.closed == 2


def _make_identity_client_cls():
    """A subclass whose identity is ``url`` alone, with ``size`` a build option."""

    class _Conn:
        def __init__(self, kwargs):
            self.kwargs = kwargs
            self.closed = False

    class _IdentityClient(PooledClient):
        created = 0
        closed = 0

        def _identity(self, **kwargs):
            return {"url": kwargs["url"]}

        async def _create(self, **kwargs):
            type(self).created += 1
            return _Conn(kwargs)

        async def _close(self, client):
            type(self).closed += 1
            client.closed = True

    return _IdentityClient


async def test_default_identity_is_all_kwargs():
    # The base identity is every non-passenger kwarg, so a differing kwarg keys a different pool.
    cls = _make_client_cls()
    base = cls()
    assert base._identity(url="a", size=1) == {"url": "a", "size": 1}
    async with cls().current(url="a", size=1) as c1, cls().current(url="a", size=2) as c2:
        assert c1 is not c2  # size is identity for the base -> a separate pool


async def test_identity_shares_pool_across_build_options():
    # Same identity, same build options: one pooled client, reused (identical
    # re-request shares the entry).
    cls = _make_identity_client_cls()
    async with cls().current(url="a", size=1) as c1, cls().current(url="a", size=1) as c2:
        assert c1 is c2
    assert cls.created == 1


async def test_conflicting_build_options_for_one_identity_raise_naming_both():
    # Same identity, different build options: a configuration conflict raised loudly,
    # naming both the recorded and the requested option sets — never a second pool.
    cls = _make_identity_client_cls()
    async with cls().current(url="a", size=1):
        with pytest.raises(ValueError, match="build options") as excinfo:
            async with cls().current(url="a", size=2):
                pass
    message = str(excinfo.value)
    assert "size" in message
    assert "1" in message
    assert "2" in message
    assert cls.created == 1  # the conflicting request never built a second client


async def test_fresh_bypasses_the_conflict_guard():
    # The fresh path builds outside the cache, so two fresh clients with the same
    # identity and different build options each build independently — no conflict.
    cls = _make_identity_client_cls()
    async with (
        client_ctx(cls, fresh=True, url="a", size=1) as c1,
        client_ctx(cls, fresh=True, url="a", size=2) as c2,
    ):
        assert c1 is not c2
    assert cls.created == 2
    assert cls.closed == 2
