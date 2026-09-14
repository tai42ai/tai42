"""Toggle enabled sub-services: inline add/remove, the consent fork when scopes are
missing, the compare-and-set loser, and the origin allow-list gate."""

from __future__ import annotations

import asyncio

import pytest

from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.service.connection_service import ConcurrentConnectionUpdateError, patch_sub_services

from ..conftest import CID, make_noauth_record, make_oauth_record
from .conftest import _FANOUT, ORIGIN, REDIRECT, _noauth_multi_descriptor


async def test_patch_unchanged_raises(harness):
    _, records, _, _, _, _ = harness
    records[CID] = make_oauth_record(connection_id=CID, enabled_sub_services=["mail"])
    with pytest.raises(ValueError, match="unchanged"):
        await patch_sub_services(
            connection_id=CID,
            desired=["mail"],
            return_url="/x",
            redirect_uri=REDIRECT,
            origin=ORIGIN,
        )


async def test_patch_inline_add_when_scopes_granted(harness):
    _, records, _, _, events, _ = harness
    # cal scope already granted → inline add, no consent.
    records[CID] = make_oauth_record(
        connection_id=CID,
        enabled_sub_services=["mail"],
        granted_scopes=["mail.read", "mail.send", "cal.read"],
    )
    result = await patch_sub_services(
        connection_id=CID,
        desired=["mail", "cal"],
        return_url="/x",
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    assert result.consent_required is False
    assert "cal" in result.enabled_sub_services
    assert events["added"]
    # The inline add mutated the manifest through the pipeline ⇒ fleet report.
    assert result.fanout == _FANOUT


async def test_patch_inline_add_ignores_off_list_origin(harness):
    """An inline-only PATCH (cal's scope already granted → no consent fork, no
    redirect flow) is NOT gated on the redirect allow-list — an off-list Origin
    still commits the inline change."""
    _, records, store, _, events, _ = harness
    records[CID] = make_oauth_record(
        connection_id=CID,
        enabled_sub_services=["mail"],
        granted_scopes=["mail.read", "mail.send", "cal.read"],
    )
    result = await patch_sub_services(
        connection_id=CID,
        desired=["mail", "cal"],
        return_url="/x",
        redirect_uri=REDIRECT,
        origin="https://evil.com",
    )
    assert result.consent_required is False
    assert "cal" in result.enabled_sub_services
    assert store.puts
    assert events["added"]


async def test_patch_removal_toggles_off(harness):
    cs_mod, records, _, _, events, providers = harness
    records[CID] = make_oauth_record(
        connection_id=CID,
        enabled_sub_services=["mail", "cal"],
        granted_scopes=["mail.read", "mail.send", "cal.read"],
    )
    cs_mod.ConfigService.from_app().seed(
        descriptor=providers["acme"], enabled_sub_services=["mail", "cal"], alias="work", connection_id=CID
    )
    result = await patch_sub_services(
        connection_id=CID,
        desired=["mail"],
        return_url="/x",
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    assert result.consent_required is False
    assert result.enabled_sub_services == ["mail"]
    assert events["removed"] == ["acme_cal_work"]
    assert result.removed_manifest_entries == ["acme_cal_work"]
    # The toggle-off mutated the manifest through the pipeline ⇒ fleet report.
    assert result.fanout == _FANOUT


async def test_patch_interleaved_writers_loser_raises_instead_of_clobbering(harness, monkeypatch):
    """Two writers race the same connection with the lock unavailable (the fake
    lock never serialises, mirroring the fail-open Redis-outage posture): the
    writer whose persist runs second loses the compare-and-set, raises
    ConcurrentConnectionUpdateError, and leaves the winner's record untouched."""
    cs_mod, records, store, _, events, _ = harness
    records[CID] = make_oauth_record(
        connection_id=CID,
        enabled_sub_services=["mail"],
        granted_scopes=["mail.read", "mail.send", "cal.read"],
    )

    loser_loaded = asyncio.Event()
    loser_may_persist = asyncio.Event()
    loads = {"count": 0}
    unpaused_load = cs_mod.load_record_with_blob

    async def gated_load(cid):
        result = await unpaused_load(cid)
        loads["count"] += 1
        if loads["count"] == 1:
            # First writer pauses between its load and its persist so the
            # second writer can commit in that window.
            loser_loaded.set()
            await loser_may_persist.wait()
        return result

    monkeypatch.setattr(cs_mod, "load_record_with_blob", gated_load)

    loser = asyncio.ensure_future(
        patch_sub_services(
            connection_id=CID,
            desired=["mail", "cal"],
            return_url="/x",
            redirect_uri=REDIRECT,
            origin=ORIGIN,
        )
    )
    await loser_loaded.wait()

    winner = await patch_sub_services(
        connection_id=CID,
        desired=["cal"],
        return_url="/x",
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    assert winner.enabled_sub_services == ["cal"]
    winner_blob = store.blobs[CID]
    events_after_winner = (list(events["added"]), list(events["removed"]))

    loser_may_persist.set()
    with pytest.raises(ConcurrentConnectionUpdateError, match="re-read the connection and retry"):
        await loser

    # The loser wrote nothing: the winner's blob is intact and no further
    # manifest reconciliation ran.
    assert store.blobs[CID] == winner_blob
    assert (events["added"], events["removed"]) == (events_after_winner[0], events_after_winner[1])


async def test_patch_off_list_origin_persists_nothing(harness):
    """A consent-requiring PATCH (cal's scope is not yet granted, so it forks an
    OAuth flow) with an off-list Origin fails closed BEFORE the inline persist,
    manifest write, or flow start — the store CAS and manifest events stay
    untouched, so no partial sub-service change leaks. The typed
    RedirectUriNotAllowedError (an OAuthError) surfaces for the router to map to 400."""
    _, records, store, _flows, events, _ = harness
    records[CID] = make_oauth_record(
        connection_id=CID,
        enabled_sub_services=["mail"],
        granted_scopes=["mail.read", "mail.send"],
    )
    with pytest.raises(oauth_client.RedirectUriNotAllowedError):
        await patch_sub_services(
            connection_id=CID,
            desired=["mail", "cal"],
            return_url="/x",
            redirect_uri=REDIRECT,
            origin="https://evil.com",
        )
    assert store.puts == []
    assert events["added"] == []
    assert events["removed"] == []
    assert events["state_put"] == []


async def test_patch_provider_removed_raises_value_error(harness, monkeypatch):
    """A PATCH whose provider plugin was unregistered surfaces a typed ValueError
    the router maps to a 4xx, not a raw KeyError 500."""
    cs_mod, records, _, _, _, _ = harness
    records[CID] = make_oauth_record(connection_id=CID, enabled_sub_services=["mail"])

    def _boom(pid):
        raise KeyError(pid)

    monkeypatch.setattr(cs_mod, "get_provider", _boom)
    with pytest.raises(ValueError, match="unknown provider"):
        await patch_sub_services(
            connection_id=CID,
            desired=["mail", "cal"],
            return_url="/x",
            redirect_uri=REDIRECT,
            origin=ORIGIN,
        )


async def test_patch_consent_fork_when_scopes_missing(harness):
    cs_mod, records, _, _flows, _events, _ = harness
    # cal scope NOT granted → consent flow.
    records[CID] = make_oauth_record(
        connection_id=CID,
        enabled_sub_services=["mail"],
        granted_scopes=["mail.read", "mail.send"],
    )
    result = await patch_sub_services(
        connection_id=CID,
        desired=["mail", "cal"],
        return_url="/connectors",
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    assert result.consent_required is True
    assert result.flow_id is not None
    assert result.authorize_url is not None
    # A consent-only toggle makes no manifest change here (the fork's own completion
    # reports its broadcast), so this response honestly carries no fleet report.
    assert result.fanout is None
    assert cs_mod.ConfigService.from_app().applies == 0


async def test_patch_no_auth_toggles_on_inline_ignoring_scopes(harness):
    """A no-auth connection has no OAuth consent flow, so toggling a sub-service ON
    is always inline — even when that sub-service declares scopes the record never
    granted (the consent-fork path is reserved for kind == "oauth")."""
    _, records, _, _flows, events, providers = harness
    providers["noauthmulti"] = _noauth_multi_descriptor()
    records[CID] = make_noauth_record(
        connection_id=CID,
        provider_id="noauthmulti",
        enabled_sub_services=["main"],
    )
    result = await patch_sub_services(
        connection_id=CID,
        desired=["main", "extra"],
        return_url="/x",
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    assert result.consent_required is False
    assert result.flow_id is None
    assert "extra" in result.enabled_sub_services
    assert events["added"]
