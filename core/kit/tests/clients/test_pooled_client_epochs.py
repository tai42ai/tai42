"""Epoch-aware pooling + leases: two epochs coexist, a retired epoch's client
closes on last lease release, drain force-closes at the deadline, and a client
created during an epoch bump is closed-and-retried rather than orphaned.

All in-process — no external driver. A fresh subclass per test keeps the
module-global, class+epoch-keyed pool from leaking state across tests, and the
autouse ``_reset_client_epoch`` fixture restores the process epoch afterward.
"""

import asyncio
import logging

import pytest

from tai42_kit.clients import (
    PooledClient,
    advance_client_epoch,
    current_client_epoch,
    drain_epoch,
    shutdown_all_clients,
)


class _Conn:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False


def _make_client_cls(*, close_raises: bool = False):
    class _FakeClient(PooledClient):
        created = 0
        closed = 0

        async def _create(self, **kwargs):
            type(self).created += 1
            return _Conn(**kwargs)

        async def _close(self, client):
            type(self).closed += 1
            client.closed = True
            if close_raises:
                raise RuntimeError("close boom")

    return _FakeClient


async def test_two_epochs_coexist_same_key():
    cls = _make_client_cls()
    inst = cls()
    async with inst.current(url="a") as c0:
        advance_client_epoch()  # retire epoch 0 while its client is leased
        async with inst.current(url="a") as c1:
            assert c1 is not c0  # new epoch -> a distinct client for the same key
        # c1 is current-epoch: it stays pooled on release.
    # c0's lease released after retirement -> closed; c1 stays.
    assert cls.created == 2
    assert cls.closed == 1


async def test_retired_client_closes_on_last_lease_release():
    cls = _make_client_cls()
    inst = cls()
    async with inst.current(url="a"):
        advance_client_epoch()  # retire epoch 0 mid-lease
        assert cls.closed == 0  # a leased retired client is not closed
    assert cls.closed == 1  # closed on the release that dropped leases to zero


async def test_retired_closes_only_after_all_leases_release():
    cls = _make_client_cls()
    inst = cls()
    async with inst.current(url="a"):
        async with inst.current(url="a"):  # second lease on the same pooled client
            advance_client_epoch()
        assert cls.closed == 0  # one lease still held -> not closed
    assert cls.closed == 1  # closed only after the last lease releases


async def test_client_released_while_current_lingers_until_shutdown():
    # A client released while still current stays pooled; retiring its epoch later
    # does not retroactively close it (nothing releases a lease again). Shutdown
    # (or drain_epoch) is what reclaims such a lingering retired-epoch client.
    cls = _make_client_cls()
    inst = cls()
    async with inst.current(url="a"):
        pass  # released while epoch 0 is current -> stays pooled
    advance_client_epoch()
    assert cls.closed == 0
    await shutdown_all_clients()
    assert cls.closed == 1


async def test_shutdown_closes_every_epoch():
    cls = _make_client_cls()
    inst = cls()
    async with inst.current(url="a"):
        pass  # epoch 0
    advance_client_epoch()
    async with inst.current(url="a"):
        pass  # epoch 1
    await shutdown_all_clients()
    assert cls.closed == 2  # both epochs' pools are closed


async def test_close_targets_current_epoch_only():
    cls = _make_client_cls()
    inst = cls()
    async with inst.current(url="a"):
        pass  # pooled under epoch 0
    advance_client_epoch()
    await inst.close(url="a")  # current epoch (1) has no such client -> no-op
    assert cls.closed == 0
    await shutdown_all_clients()  # the retired-epoch client is still reclaimable
    assert cls.closed == 1


async def test_drain_epoch_closes_after_lease_releases():
    cls = _make_client_cls()
    inst = cls()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with inst.current(url="a"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    retired = advance_client_epoch()
    drain = asyncio.create_task(drain_epoch(retired, 5.0))
    await asyncio.sleep(0.02)  # drain is polling; the lease is still held
    assert cls.closed == 0
    release.set()  # lease releases -> the retired client closes on release
    await task
    await drain  # sees the epoch emptied -> returns without force-closing
    assert cls.closed == 1


async def test_drain_epoch_force_closes_at_deadline_and_raises_group():
    cls = _make_client_cls(close_raises=True)
    inst = cls()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with inst.current(url="a"):
            entered.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await entered.wait()
    retired = advance_client_epoch()
    with pytest.raises(ExceptionGroup) as ei:
        await drain_epoch(retired, 0.02)  # lease never releases -> deadline force-close
    assert cls.closed == 1  # force-closed despite the held lease
    assert len(ei.value.exceptions) == 1
    assert all(isinstance(e, RuntimeError) for e in ei.value.exceptions)
    # The still-open lease releasing afterward must not double-close.
    release.set()
    await task
    assert cls.closed == 1


async def test_drain_epoch_refuses_current_epoch():
    with pytest.raises(ValueError, match="current client epoch"):
        await drain_epoch(current_client_epoch(), 0.0)


async def test_drain_epoch_missing_is_noop():
    retired = advance_client_epoch()  # nothing was ever pooled under it
    await drain_epoch(retired, 0.0)  # returns without error


async def test_create_during_epoch_bump_is_closed_and_retried():
    # A client whose _create is in flight when the epoch advances belongs to a
    # now-retired epoch. It must be closed and the acquire retried under the fresh
    # epoch — never orphaned in a dict nothing drains.
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowClient(PooledClient):
        created = 0
        closed = 0

        async def _create(self, **kwargs):
            type(self).created += 1
            if type(self).created == 1:
                started.set()
                await release.wait()  # suspend the first create so a bump interleaves
            return _Conn(**kwargs)

        async def _close(self, client):
            type(self).closed += 1
            client.closed = True

    inst = _SlowClient()

    async def _use():
        async with inst.current(url="a"):
            pass

    task = asyncio.create_task(_use())
    await started.wait()  # first create in flight under epoch 0
    advance_client_epoch()  # retire epoch 0 mid-create
    release.set()
    await task
    # First client closed-and-retried; second (current-epoch) client pooled.
    assert _SlowClient.created == 2
    assert _SlowClient.closed == 1
    await shutdown_all_clients()
    assert _SlowClient.closed == 2  # the retried current-epoch client closes here


async def test_close_failure_on_lease_release_does_not_mask_body_exception():
    # A retired client whose _close raises on the last lease release must not mask
    # the caller body's own in-flight exception with the close error.
    cls = _make_client_cls(close_raises=True)
    inst = cls()

    async def body():
        async with inst.current(url="a"):
            advance_client_epoch()  # retire epoch 0 mid-lease
            raise ValueError("body boom")

    with pytest.raises(ValueError, match="body boom"):
        await body()
    assert cls.closed == 1  # close still attempted on release, its error swallowed


async def test_close_failure_on_lease_release_is_logged_not_raised(caplog):
    # A clean body return is unaffected: the close error on the last lease release
    # is logged, never raised into the caller's clean return path.
    cls = _make_client_cls(close_raises=True)
    inst = cls()
    with caplog.at_level(logging.ERROR, logger="tai42_kit.clients.base"):
        async with inst.current(url="a"):
            advance_client_epoch()  # retire epoch 0 mid-lease
    assert cls.closed == 1
    assert any("Error closing retired client" in r.message for r in caplog.records)


async def test_drain_force_closes_idle_retired_client_promptly():
    # An idle (0-lease) retired client fires no further release event, so drain
    # must fall straight through to force-close instead of burning the deadline
    # polling on its mere pooled presence.
    cls = _make_client_cls()
    inst = cls()
    async with inst.current(url="a"):
        pass  # released while epoch 0 is current -> pooled, idle, 0 leases
    retired = advance_client_epoch()
    loop = asyncio.get_running_loop()
    start = loop.time()
    await drain_epoch(retired, 100.0)  # a huge deadline that must NOT be consumed
    elapsed = loop.time() - start
    assert cls.closed == 1  # force-closed
    assert elapsed < 1.0  # promptly, not after the 100s deadline


async def test_current_epoch_starts_at_zero_by_default():
    # With no advance_client_epoch() caller, the epoch is 0 — the default that
    # must be behaviorally identical to the pre-epoch pooling.
    assert current_client_epoch() == 0
