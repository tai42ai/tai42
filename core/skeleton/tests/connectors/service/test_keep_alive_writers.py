"""A keep-alive never costs another writer its write: a reconnect completion and a sub-service update that
loaded the record before a keep-alive put still commit after it (no ConcurrentConnectionUpdateError)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager

import pytest
from tai42_contract.connectors.service import FlowOperation

import tai42_skeleton.connectors.runtime.resolver as resolver_mod
from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.oauth import crypto
from tai42_skeleton.connectors.runtime import refresh_owner
from tai42_skeleton.connectors.service.connection_service import complete_connect, patch_sub_services

from ..conftest import CID, make_oauth_record
from .conftest import ORIGIN, REDIRECT, _flow, _token_response


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    refresh_owner.reset()
    resolver_mod.reset_keep_alive_stamps()
    yield
    resolver_mod.reset_keep_alive_stamps()


def _share_the_store_with_the_resolver(harness, monkeypatch) -> None:
    """Point the resolver at the harness's records and compare-and-set store, with a sealed blob."""
    cs_mod, records, store, _, _, providers = harness
    record = records[CID]
    store.blobs[CID] = crypto.encrypt(record.to_storage_json().encode(), connection_id=CID)

    async def _load(connection_id):
        return records[connection_id], store.blobs[connection_id]

    @asynccontextmanager
    async def _lock(connection_id):
        yield

    async def _claim(cid, seconds):
        return True

    async def _none(cid):
        return None

    monkeypatch.setattr(resolver_mod, "load_record_with_blob", _load)
    monkeypatch.setattr(resolver_mod, "connection_lock", _lock)
    monkeypatch.setattr(resolver_mod, "token_store", lambda: store)
    monkeypatch.setattr(resolver_mod, "get_provider", lambda pid: providers[pid])
    monkeypatch.setattr(resolver_mod, "claim_keep_alive", _claim)
    monkeypatch.setattr(cs_mod, "clear_refresh_cooldown", _none)

    original_load = cs_mod.load_record_with_blob

    async def _load_then_a_peer_keeps_alive(cid, **kwargs):
        loaded = await original_load(cid, **kwargs)
        # Between this writer's load and its write-back, another caller serves the record.
        before = store.blobs[cid]
        await resolver_mod.resolve_managed_auth(cid, "acme", "mail")
        assert store.blobs[cid] == before  # the keep-alive put the same ciphertext back
        return loaded

    monkeypatch.setattr(cs_mod, "load_record_with_blob", _load_then_a_peer_keeps_alive)


async def test_a_reconnect_that_loaded_before_a_keep_alive_still_commits(harness, monkeypatch):
    _, records, store, flows, _, _ = harness
    records[CID] = make_oauth_record(connection_id=CID, enabled_sub_services=["mail"])
    _share_the_store_with_the_resolver(harness, monkeypatch)
    puts_before = len(store.puts)
    flow = _flow(operation=FlowOperation.RECONNECT, reconnect_cid=CID)
    flows[flow.flow_id] = flow

    async def _exchange(**kwargs):
        return _token_response(access_token="reconnected-at")

    monkeypatch.setattr(oauth_client, "exchange_code", _exchange)
    await complete_connect(flow_id=flow.flow_id, code="c")
    # The keep-alive and the reconnect's write both committed.
    assert len(store.puts) == puts_before + 2


async def test_a_sub_service_update_that_loaded_before_a_keep_alive_still_commits(harness, monkeypatch):
    _, records, store, _, _, _ = harness
    records[CID] = make_oauth_record(
        connection_id=CID, enabled_sub_services=["mail", "cal"], granted_scopes=["mail.read", "mail.send", "cal.read"]
    )
    _share_the_store_with_the_resolver(harness, monkeypatch)
    puts_before = len(store.puts)
    result = await patch_sub_services(
        connection_id=CID, desired=["mail"], return_url="/x", redirect_uri=REDIRECT, origin=ORIGIN
    )
    assert result.enabled_sub_services == ["mail"]
    assert len(store.puts) == puts_before + 2
