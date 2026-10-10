"""Finish a connection from the OAuth callback: fresh connect, reconnect/toggle token
replacement, the post-exchange failure revoke policy, and the write-under-lock guarantee."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.connectors.service import AliasInUseError, CompleteConnectResult, FlowOperation

from tai42_skeleton.app import instance
from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.service.connection_service import (
    ConcurrentConnectionUpdateError,
    complete_connect,
    start_connect,
)
from tai42_skeleton.operations import _broadcast
from tai42_skeleton.operations._broadcast import FleetBroadcastError

from ..._fakes.bus import FakeBus
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

    # Another connection of the same provider (its own alias, so its own entry titles).
    records[CID] = make_oauth_record(connection_id=CID, alias="home", enabled_sub_services=["mail"])
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
    # The connection's entry is in the manifest as the builder wrote it, and the reconnect
    # flow's enabled set equals the record's ⇒ nothing to append, remove or rewrite.
    cs_mod.ConfigService.from_app().seed(
        descriptor=_providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID
    )
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

    async def fake_revoke(*, descriptor, resolved, client, token):
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

    async def fake_revoke(*, descriptor, resolved, client, token):
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


# -- a reconnect asks the fleet to re-probe the connection's failed entries ----------------------


class _ReprobeAdmin:
    """The admin facet's set-scoped re-probe, recording the titles it was asked for (or failing)."""

    def __init__(self, error: Exception | None = None) -> None:
        self.asked: list[list[str]] = []
        self.error = error

    async def reprobe_failed_mcps(self, titles: list[str]) -> list[dict[str, Any]]:
        self.asked.append(list(titles))
        if self.error is not None:
            raise self.error
        return [{"title": t, "status": "ok"} for t in titles]


def _real_broadcast(monkeypatch, cs_mod, admin: _ReprobeAdmin) -> FakeBus:
    bus = FakeBus(remotes=["serve-sibling"])
    monkeypatch.setattr(instance.app, "_bus", bus)
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(admin=admin))
    monkeypatch.setattr(cs_mod, "broadcast", _broadcast.broadcast)
    return bus


def _reconnect_flow(flows, enabled: list[str]):
    flow = _flow(operation=FlowOperation.RECONNECT, reconnect_cid=CID)
    flow.enabled_sub_services = enabled
    flows[flow.flow_id] = flow
    return flow


async def _new_tokens(**kwargs):
    return _token_response(access_token="new-at")


async def test_a_reconnect_with_unchanged_entries_asks_the_fleet_to_reprobe_them(harness, monkeypatch):
    cs_mod, records, _store, flows, _events, providers = harness
    records[CID] = make_oauth_record(connection_id=CID, alias="work", enabled_sub_services=["mail", "cal"])
    cs_mod.ConfigService.from_app().seed(
        descriptor=providers["acme"], enabled_sub_services=["mail", "cal"], alias="work", connection_id=CID
    )
    admin = _ReprobeAdmin()
    bus = _real_broadcast(monkeypatch, cs_mod, admin)
    flow = _reconnect_flow(flows, ["mail", "cal"])
    monkeypatch.setattr(oauth_client, "exchange_code", _new_tokens)

    result = await complete_connect(flow_id=flow.flow_id, code="auth")

    titles = ["acme_cal_work", "acme_mail_work"]
    # No manifest change: no pipeline run and no manifest fan-out to report.
    assert cs_mod.ConfigService.from_app().applies == 0
    assert result.fanout is None
    # This worker re-probed the titles, and the same op went to the fleet.
    assert admin.asked == [titles]
    ((op, targets, local),) = bus.publish_calls
    assert op == {"op": "reprobe_failed_mcps", "titles": titles}
    assert targets is None
    assert local is not None
    assert local.outcome.value == "applied"


async def test_a_reconnect_that_changes_entries_runs_the_pipeline_then_asks_for_the_reprobe(harness, monkeypatch):
    cs_mod, records, _store, flows, events, providers = harness
    records[CID] = make_oauth_record(connection_id=CID, alias="work", enabled_sub_services=["mail"])
    cs_mod.ConfigService.from_app().seed(
        descriptor=providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID
    )
    order: list[str] = []
    service = cs_mod.ConfigService.from_app()
    original_apply = service.apply_change

    async def apply_change(mutator):
        order.append("pipeline")
        return await original_apply(mutator)

    monkeypatch.setattr(service, "apply_change", apply_change)

    async def recording_broadcast(op, targets, apply, *, publish_on_local_failure=False, pre_mutation_errors=()):
        order.append("reprobe")
        events["broadcasts"].append({"op": op, "publish_on_local_failure": publish_on_local_failure})
        return {}

    monkeypatch.setattr(cs_mod, "broadcast", recording_broadcast)
    flow = _reconnect_flow(flows, ["mail", "cal"])
    monkeypatch.setattr(oauth_client, "exchange_code", _new_tokens)

    result = await complete_connect(flow_id=flow.flow_id, code="auth")

    assert order == ["pipeline", "reprobe"]
    assert result.fanout == _FANOUT
    assert events["broadcasts"] == [
        {
            "op": {"op": "reprobe_failed_mcps", "titles": ["acme_cal_work", "acme_mail_work"]},
            "publish_on_local_failure": True,
        }
    ]


async def test_a_toggle_on_completion_asks_for_the_reprobe_too(harness, monkeypatch):
    cs_mod, records, _store, flows, events, providers = harness
    records[CID] = make_oauth_record(connection_id=CID, alias="work", enabled_sub_services=["mail"])
    cs_mod.ConfigService.from_app().seed(
        descriptor=providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID
    )
    flow = _flow(operation=FlowOperation.TOGGLE_SUBSERVICE_ON, reconnect_cid=CID)
    flow.enabled_sub_services = ["mail", "cal"]
    flows[flow.flow_id] = flow
    monkeypatch.setattr(oauth_client, "exchange_code", _new_tokens)

    await complete_connect(flow_id=flow.flow_id, code="auth")

    assert [b["op"] for b in events["broadcasts"]] == [
        {"op": "reprobe_failed_mcps", "titles": ["acme_cal_work", "acme_mail_work"]}
    ]
    assert events["broadcasts"][0]["publish_on_local_failure"] is True


async def test_a_failing_local_reprobe_still_reaches_the_fleet_and_raises(harness, monkeypatch):
    """The reconnect is already persisted, so the siblings are asked even when this worker's pass fails;
    the failure then surfaces loudly."""
    cs_mod, records, _store, flows, _events, providers = harness
    records[CID] = make_oauth_record(connection_id=CID, alias="work", enabled_sub_services=["mail"])
    cs_mod.ConfigService.from_app().seed(
        descriptor=providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID
    )
    admin = _ReprobeAdmin(error=RuntimeError("probe pass broke"))
    bus = _real_broadcast(monkeypatch, cs_mod, admin)
    flow = _reconnect_flow(flows, ["mail"])
    monkeypatch.setattr(oauth_client, "exchange_code", _new_tokens)

    with pytest.raises(FleetBroadcastError):
        await complete_connect(flow_id=flow.flow_id, code="auth")

    ((op, _targets, local),) = bus.publish_calls
    assert op == {"op": "reprobe_failed_mcps", "titles": ["acme_mail_work"]}
    assert local is not None
    assert local.outcome.value == "failed"
    assert records[CID].auth_health_state.value == "healthy"
