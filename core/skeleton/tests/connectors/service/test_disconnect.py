"""Tear down a connection: upstream revoke policy, local purge, and the lock that keeps
an in-flight patch from stranding manifest entries against a deleted record."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.service.connection_service import (
    ConnectionNotFoundError,
    disconnect,
    patch_sub_services,
)

from ..conftest import CID, make_noauth_record, make_oauth_record
from .conftest import _FANOUT, ORIGIN, REDIRECT


async def test_disconnect_no_auth_skips_revoke(harness):
    cs_mod, records, store, _, events, providers = harness
    records[CID] = make_noauth_record()
    # Seed the connection's managed entry so disconnect is observed removing it.
    cs_mod.ConfigService.from_app().seed(
        descriptor=providers["widgets"], enabled_sub_services=["search"], alias="main", connection_id=CID
    )
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "skipped"
    assert CID in store.deleted
    # The managed entry left through the pipeline (validate + reload + broadcast).
    assert events["removed"] == ["widgets_search_main"]
    assert result.removed_manifest_entries == ["widgets_search_main"]
    assert cs_mod.ConfigService.from_app().doc["mcp"] == []
    # A disconnect always removes managed entries through the pipeline ⇒ fleet report.
    assert result.fanout == _FANOUT


async def test_disconnect_oauth_revokes(harness, monkeypatch):
    _, records, store, _, _events, _ = harness
    records[CID] = make_oauth_record(connection_id=CID)

    async def fake_revoke(*, descriptor, token):
        return oauth_client.RevokeOutcome(outcome="success", http_status=200)

    monkeypatch.setattr(oauth_client, "revoke", fake_revoke)
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "success"
    assert CID in store.deleted
    # The oauth disconnect removes managed entries through the pipeline ⇒ fleet report.
    assert result.fanout == _FANOUT


async def test_disconnect_with_failed_revoke_still_purges_locally(harness, monkeypatch):
    """A best-effort upstream revoke that reports failure (not a raise) must not
    block the local purge — the blob and manifest entries are removed regardless,
    and the failed outcome is surfaced on the result."""
    cs_mod, records, store, _, events, providers = harness
    records[CID] = make_oauth_record(connection_id=CID)
    cs_mod.ConfigService.from_app().seed(
        descriptor=providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID
    )

    async def fake_revoke(*, descriptor, token):
        return oauth_client.RevokeOutcome(outcome="failed", http_status=500)

    monkeypatch.setattr(oauth_client, "revoke", fake_revoke)
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "failed"
    assert result.upstream_revoke_status == 500
    assert CID in store.deleted
    assert events["removed"] == ["acme_mail_work"]


async def test_disconnect_provider_gone_purges_locally(harness, monkeypatch):
    """With the provider plugin unregistered, get_provider raises KeyError;
    disconnect skips the upstream revoke and still purges the blob + manifest so a
    retry is not wedged at 500 forever."""
    cs_mod, records, store, _, events, providers = harness
    records[CID] = make_oauth_record(connection_id=CID)
    # Seed before the provider disappears — removal keys off the manifest's own
    # managed back-reference, not a live provider lookup.
    cs_mod.ConfigService.from_app().seed(
        descriptor=providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID
    )

    def _boom(pid):
        raise KeyError(pid)

    monkeypatch.setattr(cs_mod, "get_provider", _boom)

    async def fake_revoke(*, descriptor, token):
        raise AssertionError("must not revoke when the provider is gone")

    monkeypatch.setattr(oauth_client, "revoke", fake_revoke)
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "skipped"
    assert result.upstream_revoke_status is None
    assert CID in store.deleted
    assert events["removed"] == ["acme_mail_work"]


async def test_disconnect_purges_expired_connection(harness, monkeypatch):
    """An EXPIRED connection must stay cleanable: its session has lapsed (a
    serving load reads it as missing), but disconnect loads with include_expired
    so the blob + manifest entries are still revoked and purged instead of
    stranded forever."""
    cs_mod, records, store, _, events, providers = harness
    records[CID] = make_oauth_record(connection_id=CID)
    store.expired.add(CID)
    cs_mod.ConfigService.from_app().seed(
        descriptor=providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID
    )

    # Sanity: the default serving load no longer surfaces the record.
    with pytest.raises(ConnectionNotFoundError):
        await cs_mod.load_record(CID)

    async def fake_revoke(*, descriptor, token):
        return oauth_client.RevokeOutcome(outcome="success", http_status=200)

    monkeypatch.setattr(oauth_client, "revoke", fake_revoke)
    result = await disconnect(connection_id=CID)
    assert result.upstream_revoke_outcome == "success"
    assert CID in store.deleted
    assert events["removed"] == ["acme_mail_work"]


async def test_disconnect_under_lock_blocks_patch_from_stranding(harness, monkeypatch):
    """Disconnect holds the connection lock across its manifest removal +
    record delete, so an in-flight patch (whose manifest add is now also under
    the lock) cannot slip its add in after the record is gone. The patch queued
    behind the lock reloads a deleted record and raises — stranding nothing."""
    cs_mod, records, store, _, events, _ = harness
    records[CID] = make_oauth_record(
        connection_id=CID,
        enabled_sub_services=["mail"],
        granted_scopes=["mail.read", "mail.send", "cal.read"],
    )

    real_lock = asyncio.Lock()
    gate = asyncio.Event()

    @asynccontextmanager
    async def serializing_lock(cid):
        async with real_lock:
            yield

    monkeypatch.setattr(cs_mod, "connection_lock", serializing_lock)

    async def fake_revoke(*, descriptor, token):
        await gate.wait()  # park disconnect INSIDE the lock until released
        return oauth_client.RevokeOutcome(outcome="success", http_status=200)

    monkeypatch.setattr(oauth_client, "revoke", fake_revoke)

    disc = asyncio.ensure_future(disconnect(connection_id=CID))
    await asyncio.sleep(0)  # let disconnect acquire the lock and park in revoke
    patch_task = asyncio.ensure_future(
        patch_sub_services(
            connection_id=CID, desired=["mail", "cal"], return_url="/x", redirect_uri=REDIRECT, origin=ORIGIN
        )
    )
    await asyncio.sleep(0)  # patch now blocks on the lock held by disconnect
    gate.set()  # release disconnect → it removes entries + deletes, frees the lock
    await disc

    # patch then acquires the lock, reloads the now-deleted record and raises —
    # never running its manifest add, so nothing is stranded.
    with pytest.raises(ConnectionNotFoundError):
        await patch_task
    assert CID in store.deleted
    assert events["added"] == []
