"""Finish a connection from the OAuth callback: fresh connect, reconnect/toggle token
replacement, the post-exchange failure revoke policy, and the write-under-lock guarantee."""

from __future__ import annotations

import pytest
from tai42_contract.connectors.service import AliasInUseError, CompleteConnectResult, FlowOperation

from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.service.connection_service import (
    ConcurrentConnectionUpdateError,
    complete_connect,
    start_connect,
)

from ..conftest import CID, make_oauth_record
from .conftest import _FANOUT, ORIGIN, REDIRECT, _depth_lock, _flow, _token_response


async def test_complete_connect_alias_in_use(harness, monkeypatch):
    """Two concurrent oauth flows with the same alias both pass start_connect;
    the loser's create-only insert trips the durable uniqueness at completion."""
    _, _, store, flows, _, _ = harness
    store.aliases[("acme", "work")] = "existing-cid"
    flow = _flow()
    flows[flow.flow_id] = flow

    async def fake_exchange(**kwargs):
        return _token_response()

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    with pytest.raises(AliasInUseError):
        await complete_connect(flow_id=flow.flow_id, code="auth")


async def test_complete_connect_unknown_flow(harness, monkeypatch):
    with pytest.raises(oauth_client.OAuthError, match="state mismatch"):
        await complete_connect(flow_id="no-such", code="c")


async def test_complete_connect_creates_record(harness, monkeypatch):
    _, _records, store, flows, events, _ = harness
    flow = _flow()
    flows[flow.flow_id] = flow

    async def fake_exchange(**kwargs):
        return _token_response()

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    result = await complete_connect(flow_id=flow.flow_id, code="auth")
    assert isinstance(result, CompleteConnectResult)
    assert result.operation == FlowOperation.CONNECT
    assert store.puts
    assert store.puts[0][1] is True
    assert events["added"]
    # The fresh-connect manifest-add always crosses the pipeline ⇒ fleet report.
    assert result.fanout == _FANOUT


async def test_require_refresh_token_true_for_connect_false_for_reconnect(harness, monkeypatch):
    """A fresh CONNECT demands the provider return a refresh_token; a RECONNECT /
    TOGGLE inherits the existing one, so the exchange must NOT require it."""
    _, records, _store, flows, _events, _ = harness
    seen: list[bool] = []

    async def fake_exchange(**kwargs):
        seen.append(kwargs["require_refresh_token"])
        return _token_response()

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)

    connect_flow = _flow(operation=FlowOperation.CONNECT)
    flows[connect_flow.flow_id] = connect_flow
    await complete_connect(flow_id=connect_flow.flow_id, code="auth")
    assert seen == [True]

    records[CID] = make_oauth_record(connection_id=CID, alias="work", enabled_sub_services=["mail"])
    reconnect_flow = _flow(operation=FlowOperation.RECONNECT, reconnect_cid=CID)
    flows[reconnect_flow.flow_id] = reconnect_flow
    await complete_connect(flow_id=reconnect_flow.flow_id, code="auth")
    assert seen == [True, False]


async def test_complete_connect_reconnect_replaces_tokens(harness, monkeypatch):
    cs_mod, records, _store, flows, events, providers = harness
    existing = make_oauth_record(
        connection_id=CID,
        alias="work",
        enabled_sub_services=["mail"],
        granted_scopes=["mail.read", "mail.send"],
    )
    records[CID] = existing
    # Seed the prior mail entry so the reconnect's toggle-off is observed leaving.
    cs_mod.ConfigService.from_app().seed(
        descriptor=providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID
    )
    flow = _flow(operation=FlowOperation.RECONNECT, reconnect_cid=CID)
    flow.enabled_sub_services = ["cal"]  # drop mail, add cal
    flows[flow.flow_id] = flow

    async def fake_exchange(**kwargs):
        return _token_response(access_token="new-at", granted_scopes=["cal.read"])

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    result = await complete_connect(flow_id=flow.flow_id, code="auth")
    assert result.connection_id == CID
    assert existing.access_token is not None
    assert existing.access_token.get_secret_value() == "new-at"
    assert existing.enabled_sub_services == ["cal"]
    # mail removed + cal added in ONE pipeline transaction (single mutator).
    assert result.removed_manifest_entries == ["acme_mail_work"]
    assert result.added_manifest_entries == ["acme_cal_work"]
    assert events["removed"] == ["acme_mail_work"]
    assert events["added"] == ["acme_cal_work"]
    # A single apply_change ⇒ one validate + reload + broadcast for remove+add.
    assert cs_mod.ConfigService.from_app().applies == 1
    # The remove+add mutated the manifest, so the fleet report rides back.
    assert result.fanout == _FANOUT


async def test_complete_reconnect_no_sub_service_delta_carries_no_fanout(harness, monkeypatch):
    """A reconnect that changes no sub-services rotates tokens only — no manifest
    mutation runs, so the result honestly carries no fleet report (``fanout`` is
    ``None``)."""
    cs_mod, records, _store, flows, _events, _providers = harness
    records[CID] = make_oauth_record(
        connection_id=CID,
        alias="work",
        enabled_sub_services=["mail"],
        granted_scopes=["mail.read", "mail.send"],
    )
    # The reconnect flow's enabled set equals the record's ⇒ no add/remove delta.
    flow = _flow(operation=FlowOperation.RECONNECT, reconnect_cid=CID)
    flows[flow.flow_id] = flow

    async def fake_exchange(**kwargs):
        return _token_response(access_token="new-at")

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    result = await complete_connect(flow_id=flow.flow_id, code="auth")

    assert result.added_manifest_entries == []
    assert result.removed_manifest_entries == []
    assert result.fanout is None
    assert cs_mod.ConfigService.from_app().applies == 0


async def test_reconnect_clears_refresh_cooldown(harness, monkeypatch):
    # An explicit reconnect restores fresh tokens + HEALTHY, so it must drop any
    # refresh-cooldown breaker a prior failing run armed.
    cs_mod, records, _store, flows, _events, _ = harness
    cleared: list[str] = []

    async def fake_clear(cid):
        cleared.append(cid)

    monkeypatch.setattr(cs_mod, "clear_refresh_cooldown", fake_clear)

    records[CID] = make_oauth_record(
        connection_id=CID,
        alias="work",
        enabled_sub_services=["mail"],
        granted_scopes=["mail.read", "mail.send"],
    )
    flow = _flow(operation=FlowOperation.RECONNECT, reconnect_cid=CID)
    flow.enabled_sub_services = ["mail"]
    flows[flow.flow_id] = flow

    async def fake_exchange(**kwargs):
        return _token_response(access_token="new-at")

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    await complete_connect(flow_id=flow.flow_id, code="auth")
    assert cleared == [CID]


async def test_complete_connect_revalidates_redirect_uri(harness, monkeypatch):
    """The stored redirect_uri is re-validated against the allow-list at token
    exchange, so a value that fell off the allow-list since authorize-start is
    rejected before any code is exchanged."""
    _, _, _, flows, _, _ = harness
    flow = _flow(redirect_uri="https://evil.example.net/oauth-bridge.html")
    flows[flow.flow_id] = flow

    async def fake_exchange(**kwargs):
        raise AssertionError("must not exchange against an off-list redirect_uri")

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    with pytest.raises(oauth_client.RedirectUriNotAllowedError):
        await complete_connect(flow_id=flow.flow_id, code="auth")


async def test_complete_connect_uses_stored_redirect_uri(harness, monkeypatch):
    """The exchange re-sends the flow-state redirect_uri byte-identically, never a
    value recomputed from the completion request."""
    _, _, _, flows, _, _ = harness
    flow = _flow(redirect_uri=REDIRECT)
    flows[flow.flow_id] = flow
    seen: dict[str, str] = {}

    async def fake_exchange(**kwargs):
        seen["redirect_uri"] = kwargs["redirect_uri"]
        return _token_response()

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    await complete_connect(flow_id=flow.flow_id, code="auth")
    assert seen["redirect_uri"] == REDIRECT


async def test_complete_connect_reconnect_without_cid_raises(harness, monkeypatch):
    _, _, _, flows, _, _ = harness
    flow = _flow(operation=FlowOperation.RECONNECT, reconnect_cid=None)
    flows[flow.flow_id] = flow

    async def fake_exchange(**kwargs):
        return _token_response()

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    with pytest.raises(oauth_client.OAuthError, match="requires reconnect_connection_id"):
        await complete_connect(flow_id=flow.flow_id, code="auth")


async def test_complete_connect_provider_removed_mid_flow(harness, monkeypatch):
    """A provider unregistered between authorize-start and completion surfaces as
    a typed OAuthError (the router maps it to a 4xx failed body, not a 500)."""
    cs_mod, _, _, flows, _, _ = harness
    flow = _flow()
    flows[flow.flow_id] = flow

    def _boom(pid):
        raise KeyError(pid)

    monkeypatch.setattr(cs_mod, "get_provider", _boom)
    with pytest.raises(oauth_client.OAuthError, match="no longer registered"):
        await complete_connect(flow_id=flow.flow_id, code="auth")


async def test_fresh_connect_failure_revokes_grant(harness, monkeypatch):
    """A fresh CONNECT whose create-only persist fails (alias collision) revokes
    the just-issued, unshared grant so the upstream consent is not orphaned."""
    _, _, store, flows, _, _ = harness
    store.aliases[("acme", "work")] = "existing-cid"  # collide the create-only insert
    flow = _flow()
    flows[flow.flow_id] = flow

    revoked: list[str] = []

    async def fake_exchange(**kwargs):
        return _token_response(refresh_token="fresh-rt")

    async def fake_revoke(*, descriptor, token):
        revoked.append(token)
        return oauth_client.RevokeOutcome(outcome="success", http_status=200)

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    monkeypatch.setattr(oauth_client, "revoke", fake_revoke)
    with pytest.raises(AliasInUseError):
        await complete_connect(flow_id=flow.flow_id, code="auth")
    assert revoked == ["fresh-rt"]


async def test_reconnect_cas_miss_does_not_revoke(harness, monkeypatch):
    """A reconnect completion that loses the CAS wrote nothing; the surviving
    connection keeps its (possibly shared, non-rotating) refresh token, so the
    reconnect path must NOT revoke — that would kill a live connection."""
    cs_mod, records, store, flows, _, _ = harness
    records[CID] = make_oauth_record(
        connection_id=CID,
        alias="work",
        enabled_sub_services=["mail"],
        granted_scopes=["mail.read", "mail.send"],
    )
    flow = _flow(operation=FlowOperation.RECONNECT, reconnect_cid=CID)
    flow.enabled_sub_services = ["mail"]
    flows[flow.flow_id] = flow

    orig_load = cs_mod.load_record_with_blob

    async def racing_load(cid):
        rec, blob = await orig_load(cid)
        store.blobs[cid] = b"rotated-by-peer"  # a peer writes between our load and persist
        return rec, blob

    monkeypatch.setattr(cs_mod, "load_record_with_blob", racing_load)

    revoked: list[str] = []

    async def fake_revoke(*, descriptor, token):
        revoked.append(token)
        return oauth_client.RevokeOutcome(outcome="success")

    async def fake_exchange(**kwargs):
        return _token_response(access_token="new-at")

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    monkeypatch.setattr(oauth_client, "revoke", fake_revoke)
    with pytest.raises(ConcurrentConnectionUpdateError):
        await complete_connect(flow_id=flow.flow_id, code="auth")
    assert revoked == []


async def test_fresh_connect_persist_and_manifest_locked(harness, monkeypatch):
    """The fresh CONNECT persists + adds manifest entries INSIDE connection_lock,
    so a concurrent disconnect cannot strand the added entries."""
    cs_mod, _, store, flows, _, _ = harness
    flow = _flow()
    flows[flow.flow_id] = flow
    depth = {"n": 0}
    put_held: list[bool] = []
    add_held: list[bool] = []

    monkeypatch.setattr(cs_mod, "connection_lock", lambda cid: _depth_lock(depth, cid))

    orig_put = store.put

    async def spy_put(*a, **k):
        put_held.append(depth["n"] > 0)
        return await orig_put(*a, **k)

    store.put = spy_put
    real_add = cs_mod.manifest_writer.add_managed_entries

    def spy_add(document, **kwargs):
        add_held.append(depth["n"] > 0)
        return real_add(document, **kwargs)

    monkeypatch.setattr(cs_mod.manifest_writer, "add_managed_entries", spy_add)

    async def fake_exchange(**kwargs):
        return _token_response()

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    await complete_connect(flow_id=flow.flow_id, code="auth")
    assert put_held == [True]
    assert add_held == [True]


async def test_no_auth_connect_persist_and_manifest_locked(harness, monkeypatch):
    """The no-auth create path also persists + adds manifest entries under the
    connection lock."""
    cs_mod, _, store, _, _, _ = harness
    depth = {"n": 0}
    put_held: list[bool] = []
    add_held: list[bool] = []

    monkeypatch.setattr(cs_mod, "connection_lock", lambda cid: _depth_lock(depth, cid))

    orig_put = store.put

    async def spy_put(*a, **k):
        put_held.append(depth["n"] > 0)
        return await orig_put(*a, **k)

    store.put = spy_put
    real_add = cs_mod.manifest_writer.add_managed_entries

    def spy_add(document, **kwargs):
        add_held.append(depth["n"] > 0)
        return real_add(document, **kwargs)

    monkeypatch.setattr(cs_mod.manifest_writer, "add_managed_entries", spy_add)
    await start_connect(
        provider_id="widgets",
        alias="main",
        enabled_sub_services=["search"],
        config_values={"api_key": "k"},
        return_url="/x",
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    assert put_held == [True]
    assert add_held == [True]
