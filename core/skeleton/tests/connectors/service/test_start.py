"""Begin a connection: start_connect (OAuth + no-auth) and start_reconnect."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from tai42_contract.connectors.service import AliasInUseError, NoAuthConnectResult, StartConnectResult

from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.oauth.state import decode as decode_state
from tai42_skeleton.connectors.service.connection_service import start_connect, start_reconnect

from ..conftest import CID, make_noauth_record, make_oauth_record
from .conftest import _FANOUT, ORIGIN, REDIRECT


async def test_start_connect_unknown_provider(harness):
    with pytest.raises(ValueError, match="unknown provider"):
        await start_connect(
            provider_id="nope",
            alias="a",
            enabled_sub_services=["mail"],
            return_url="/x",
            redirect_uri=REDIRECT,
            origin=ORIGIN,
        )


async def test_start_connect_bad_alias(harness):
    with pytest.raises(ValueError, match="alias must be"):
        await start_connect(
            provider_id="acme",
            alias="Bad Alias!",
            enabled_sub_services=["mail"],
            return_url="/x",
            redirect_uri=REDIRECT,
            origin=ORIGIN,
        )


async def test_start_connect_unknown_sub_service(harness):
    with pytest.raises(ValueError, match="unknown sub-services"):
        await start_connect(
            provider_id="acme",
            alias="work",
            enabled_sub_services=["bogus"],
            return_url="/x",
            redirect_uri=REDIRECT,
            origin=ORIGIN,
        )


async def test_start_connect_oauth_returns_authorize_url(harness):
    result = await start_connect(
        provider_id="acme",
        alias="work",
        enabled_sub_services=["mail"],
        return_url="/connectors",
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    assert isinstance(result, StartConnectResult)
    assert "acme.test/authorize" in result.authorize_url
    _, _, _, _flows, events, _ = harness
    assert events["state_put"]  # flow persisted


async def test_start_connect_signs_origin_into_recoverable_state(harness):
    """The deployment origin is signed into the authorize-URL ``state`` so a
    callback routed through the OAuth bridge can decode it and bounce the code
    back here — decoding the emitted state must recover exactly that origin."""
    result = await start_connect(
        provider_id="acme",
        alias="work",
        enabled_sub_services=["mail"],
        return_url="/connectors",
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    assert isinstance(result, StartConnectResult)
    encoded_state = parse_qs(urlparse(result.authorize_url).query)["state"][0]
    decoded = decode_state(encoded_state)
    assert decoded.origin == ORIGIN
    assert decoded.flow_id == result.flow_id


async def test_start_connect_oauth_rejects_config_values(harness):
    with pytest.raises(ValueError, match="config_values are not accepted"):
        await start_connect(
            provider_id="acme",
            alias="work",
            enabled_sub_services=["mail"],
            config_values={"x": "y"},
            return_url="/x",
            redirect_uri=REDIRECT,
            origin=ORIGIN,
        )


async def test_start_connect_no_auth_alias_in_use(harness):
    """A no-auth connect creates immediately, so a duplicate (provider, alias)
    trips the store's durable uniqueness authority and surfaces AliasInUseError."""
    _, _, store, _, _, _ = harness
    store.aliases[("widgets", "main")] = "existing-cid"
    with pytest.raises(AliasInUseError):
        await start_connect(
            provider_id="widgets",
            alias="main",
            enabled_sub_services=["search"],
            config_values={"api_key": "k"},
            return_url="/x",
            redirect_uri=REDIRECT,
            origin=ORIGIN,
        )


async def test_start_connect_no_auth_creates_immediately(harness):
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
    _, _, store, _, events, _ = harness
    assert store.puts
    assert store.puts[0][1] is True
    assert events["added"]
    # The manifest-add always crosses the pipeline, so the fleet report rides back.
    assert result.fanout == _FANOUT


async def test_start_connect_no_auth_ignores_off_list_origin(harness):
    """A no-auth connect has no redirect flow, so it is NOT gated on the redirect
    allow-list — an off-list Origin still creates the connection immediately."""
    result = await start_connect(
        provider_id="widgets",
        alias="main",
        enabled_sub_services=["search"],
        config_values={"api_key": "k"},
        return_url="/x",
        redirect_uri=REDIRECT,
        origin="https://evil.com",
    )
    assert isinstance(result, NoAuthConnectResult)
    _, _, store, _, events, _ = harness
    assert store.puts
    assert events["added"]


async def test_start_connect_rejects_off_list_origin(harness):
    """A spoofed Origin not on the redirect allow-list is rejected before any
    flow state is persisted (fail-closed)."""
    _, _, _, _flows, events, _ = harness
    with pytest.raises(oauth_client.RedirectUriNotAllowedError):
        await start_connect(
            provider_id="acme",
            alias="work",
            enabled_sub_services=["mail"],
            return_url="/x",
            redirect_uri=REDIRECT,
            origin="https://evil.com",
        )
    assert events["state_put"] == []


async def test_start_connect_rejects_websocket_sub_service(harness):
    """A websocket sub-service probes healthy but every managed call raises, so a
    Connect for it is rejected at validation (fail-loud) before any OAuth flow."""
    from tai42_contract.connectors.providers import (
        McpServerDescriptor,
        ProviderDescriptor,
        SubServiceDescriptor,
    )

    _, _, _, _, _, providers = harness
    providers["wsprov"] = ProviderDescriptor(
        id="wsprov",
        display_name="WS",
        icon_url="https://ws.test/icon.png",
        kind="none",
        origin="system",
        category="data",
        sub_services={
            "live": SubServiceDescriptor(
                id="live",
                display_name="Live",
                mcp_server=McpServerDescriptor(type="websocket", url="wss://ws.test/mcp"),
            ),
        },
    )
    with pytest.raises(ValueError, match="transport 'websocket'"):
        await start_connect(
            provider_id="wsprov",
            alias="live",
            enabled_sub_services=["live"],
            config_values={},
            return_url="/x",
            redirect_uri=REDIRECT,
            origin=ORIGIN,
        )


async def test_start_reconnect_no_auth_rejected(harness):
    _, records, _, _, _, _ = harness
    records[CID] = make_noauth_record()
    with pytest.raises(ValueError, match="cannot be reconnected"):
        await start_reconnect(
            connection_id=CID,
            enabled_sub_services=["search"],
            return_url="/x",
            redirect_uri=REDIRECT,
            origin=ORIGIN,
        )


async def test_start_reconnect_oauth(harness):
    _, records, _, _, _, _ = harness
    records[CID] = make_oauth_record(alias="work")
    result = await start_reconnect(
        connection_id=CID,
        enabled_sub_services=["mail", "cal"],
        return_url="/connectors",
        redirect_uri=REDIRECT,
        origin=ORIGIN,
    )
    assert isinstance(result, StartConnectResult)


async def test_start_reconnect_provider_removed_raises_value_error(harness, monkeypatch):
    """A reconnect whose provider plugin was unregistered surfaces a typed
    ValueError the router maps to a 4xx, not a raw KeyError 500."""
    cs_mod, records, _, _, _, _ = harness
    records[CID] = make_oauth_record(connection_id=CID, alias="work")

    def _boom(pid):
        raise KeyError(pid)

    monkeypatch.setattr(cs_mod, "get_provider", _boom)
    with pytest.raises(ValueError, match="unknown provider"):
        await start_reconnect(
            connection_id=CID,
            enabled_sub_services=["mail"],
            return_url="/x",
            redirect_uri=REDIRECT,
            origin=ORIGIN,
        )


async def test_start_reconnect_rejects_off_list_origin(harness):
    """A spoofed Origin not on the redirect allow-list is rejected inside
    _start_flow before any flow state is persisted — reconnect is always an OAuth
    redirect, so nothing is written when the origin fails closed."""
    _, records, store, _flows, events, _ = harness
    records[CID] = make_oauth_record(connection_id=CID, alias="work")
    with pytest.raises(oauth_client.RedirectUriNotAllowedError):
        await start_reconnect(
            connection_id=CID,
            enabled_sub_services=["mail"],
            return_url="/x",
            redirect_uri=REDIRECT,
            origin="https://evil.com",
        )
    assert store.puts == []
    assert events["state_put"] == []
