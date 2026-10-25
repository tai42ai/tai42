"""Every connection writer reports the config store's span-release failure beside its fan-out.

The manifest write persisted, so the connection's change stands and the writer returns its
result; the store's error rides on the result's ``span_release_error``.
"""

from __future__ import annotations

from tai42_contract.connectors.service import FlowOperation, NoAuthConnectResult

from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.service.connection_service import (
    complete_connect,
    disconnect,
    patch_sub_services,
    start_connect,
)

from ..conftest import CID, make_noauth_record, make_oauth_record
from .conftest import _FANOUT, ORIGIN, REDIRECT, _flow, _token_response

_RELEASE_ERROR = "OSError: releasing the span failed"


def _failing_release(harness) -> None:
    cs_mod = harness[0]
    cs_mod.ConfigService.from_app().span_release_error = _RELEASE_ERROR


async def test_a_no_auth_connect_reports_the_span_release_failure(harness):
    _failing_release(harness)
    result = await start_connect(
        provider_id="widgets",
        alias="main",
        enabled_sub_services=["search"],
        config_values={"api_key": "k"},
        return_url="/x",
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    assert isinstance(result, NoAuthConnectResult)
    assert result.fanout == _FANOUT
    assert result.span_release_error == _RELEASE_ERROR


async def test_an_oauth_connect_completion_reports_the_span_release_failure(harness, monkeypatch):
    _failing_release(harness)
    flows = harness[3]
    flow = _flow()
    flows[flow.flow_id] = flow

    async def fake_exchange(**kwargs):
        return _token_response()

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    result = await complete_connect(flow_id=flow.flow_id, code="auth")
    assert result.fanout == _FANOUT
    assert result.span_release_error == _RELEASE_ERROR


async def test_a_reconnect_completion_reports_the_span_release_failure(harness, monkeypatch):
    cs_mod, records, _store, flows, _events, providers = harness
    _failing_release(harness)
    records[CID] = make_oauth_record(
        connection_id=CID, alias="work", enabled_sub_services=["mail"], granted_scopes=["mail.read", "mail.send"]
    )
    cs_mod.ConfigService.from_app().seed(
        descriptor=providers["acme"], enabled_sub_services=["mail"], alias="work", connection_id=CID
    )
    flow = _flow(operation=FlowOperation.RECONNECT, reconnect_cid=CID)
    flow.enabled_sub_services = ["cal"]
    flows[flow.flow_id] = flow

    async def fake_exchange(**kwargs):
        return _token_response(access_token="new-at", granted_scopes=["cal.read"])

    monkeypatch.setattr(oauth_client, "exchange_code", fake_exchange)
    result = await complete_connect(flow_id=flow.flow_id, code="auth")
    assert result.fanout == _FANOUT
    assert result.span_release_error == _RELEASE_ERROR


async def test_an_inline_sub_service_toggle_reports_the_span_release_failure(harness):
    records = harness[1]
    _failing_release(harness)
    records[CID] = make_oauth_record(
        connection_id=CID, enabled_sub_services=["mail"], granted_scopes=["mail.read", "mail.send", "cal.read"]
    )
    result = await patch_sub_services(
        connection_id=CID, desired=["mail", "cal"], return_url="/x", redirect_uri=REDIRECT, origin=ORIGIN
    )
    assert result.consent_required is False
    assert result.fanout == _FANOUT
    assert result.span_release_error == _RELEASE_ERROR


async def test_a_disconnect_reports_the_span_release_failure(harness):
    cs_mod, records, _store, _flows, _events, providers = harness
    _failing_release(harness)
    records[CID] = make_noauth_record()
    cs_mod.ConfigService.from_app().seed(
        descriptor=providers["widgets"], enabled_sub_services=["search"], alias="main", connection_id=CID
    )
    result = await disconnect(connection_id=CID)
    assert result.fanout == _FANOUT
    assert result.span_release_error == _RELEASE_ERROR
