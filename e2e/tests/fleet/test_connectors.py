"""The connector manifest-writing paths converge the fleet.

Every connector mutation rides the same ``manifest_writer`` → ConfigService pipeline
that fans a ``reload_config`` out over the worker bus, so a connect / disconnect /
oauth-complete on ONE worker converges EVERY worker with NO manual reload. The
observable is the REAL managed MCP server (``tai42_e2e_fixtures.managed_mcp_server``,
launched by each fixture provider's sub-service): its ``ping`` tool binds fleet-wide,
proven by the per-worker probe digest AND by the tool appearing on the served surface.

* connect a no-auth provider → managed tools visible on ALL workers; disconnect →
  gone on all workers (the stale-serving regression).
* PATCH the two-sub-service ``e2e_noauth_multi`` provider to toggle ONE sub-service
  off (leaving the other enabled) → just that sub-service's tools gone on all workers;
  toggle it back on → they return on all workers.
* OAuth-complete + reconnect via ``e2e_idp`` (whose sub-service rides the same
  launchable server) → convergence.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from _fleet import (
    assert_fleet_fanout,
    build_fleet_connectors_stack,
    connectors_resource_kwargs,
    converged_baseline,
    converged_digest,
    wait_present,
)

from tai42_e2e import wait_for_async
from tai42_e2e.netfixtures import OAuthIdp
from tai42_e2e.stack import TaiStack, _probe_tolerating_reloading

pytestmark = pytest.mark.backendless


async def _manifest_mcp_titles(stack: TaiStack) -> list[str]:
    manifest = await stack.api().get("/api/manifest", retry_on_reloading=True)
    return [entry["title"] for entry in manifest["mcp"]]


async def _serves(stack: TaiStack, tool: str) -> bool:
    # A connect/disconnect fans a ``reload_config`` out over the fleet; even after
    # ``converged_digest`` confirms the mutation landed, a worker's deferred session-manager
    # close can still retire a freshly-opened MCP session mid-listing (D13a — the swapped-in
    # epoch serves a new session-id space -> "Session terminated"). Re-open on a fresh
    # session until the listing lands, so this returns the TRUE presence (never a
    # session-swap sentinel — ``_not_serves`` inverts it, so a collapsed False would falsely
    # report the tool gone).
    async def _thunk() -> bool:
        async with stack.mcp() as mcp:
            return tool in await mcp.tool_names()

    return await wait_for_async(
        lambda: _probe_tolerating_reloading(_thunk),
        deadline=10.0,
        message=f"the tool listing for {tool!r} never left the reload/session-swap window",
    )


async def _authorize(authorize_url: str) -> tuple[str, str]:
    """Follow the authorize URL to the stub IdP and recover (code, state) from its
    redirect back to the connector callback."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
        response = await client.get(authorize_url)
    query = parse_qs(urlparse(response.headers["location"]).query)
    return query["code"][0], query["state"][0]


def _connectors_stack(fresh_stack: Callable[..., TaiStack], oauth_idp: OAuthIdp) -> TaiStack:
    return fresh_stack(build_fleet_connectors_stack, resource_kwargs=connectors_resource_kwargs(oauth_idp.base_url))


async def test_noauth_connect_disconnect_converges(
    fresh_stack: Callable[..., TaiStack], oauth_idp: OAuthIdp, uniq: Callable[[str], str]
) -> None:
    """Connecting the no-auth ``e2e_noauth_alpha`` provider makes its managed
    server's tools visible on EVERY worker with no manual reload; disconnecting removes
    them on every worker (the stale-serving regression). The connector-written mcp
    entry is present/absent in ``GET /api/manifest`` as the persisted doc says."""
    stack = _connectors_stack(fresh_stack, oauth_idp)
    api = stack.api()
    baseline = await converged_baseline(stack)

    alias = uniq("conn")
    start = await api.post(
        "/api/connectors/connections/start",
        json={"provider_id": "e2e_noauth_alpha", "alias": alias, "enabled_sub_services": ["default"]},
        retry_on_reloading=True,
    )
    connection_id = start["connection_id"]
    entry_title = start["added_manifest_entries"][0]
    assert_fleet_fanout(start)

    connected = await converged_digest(stack, differ_from=baseline)
    assert entry_title in await _manifest_mcp_titles(stack)  # live-vs-persisted projection
    tool = f"{entry_title}_ping"
    await wait_present(
        lambda: _serves(stack, tool), deadline=30.0, message=f"managed tool {tool} never served fleet-wide"
    )

    # Disconnect → the manifest entry and its tools are gone on every worker.
    disconnect = await api.delete(f"/api/connectors/connections/{connection_id}", retry_on_reloading=True)
    assert_fleet_fanout(disconnect)

    await converged_digest(stack, differ_from=connected)
    assert entry_title not in await _manifest_mcp_titles(stack)  # live-vs-persisted projection
    await wait_present(
        lambda: _not_serves(stack, tool), deadline=30.0, message=f"managed tool {tool} still served after disconnect"
    )


async def test_sub_service_toggle_converges(
    fresh_stack: Callable[..., TaiStack], oauth_idp: OAuthIdp, uniq: Callable[[str], str]
) -> None:
    """Connecting the two-sub-service no-auth ``e2e_noauth_multi`` provider with
    BOTH sub-services binds each one's managed tools (each under its own manifest-entry
    prefix) on EVERY worker. A PATCH toggling ONE sub-service off — leaving the other
    enabled, so the ``min_length=1`` floor holds — removes just that sub-service's tools
    on every worker; toggling it back on restores them on every worker. Each direction
    rides the ``manifest_writer`` seam and converges the fleet with no manual reload."""
    stack = _connectors_stack(fresh_stack, oauth_idp)
    api = stack.api()
    baseline = await converged_baseline(stack)

    alias = uniq("conn")
    start = await api.post(
        "/api/connectors/connections/start",
        json={"provider_id": "e2e_noauth_multi", "alias": alias, "enabled_sub_services": ["alpha", "beta"]},
        retry_on_reloading=True,
    )
    connection_id = start["connection_id"]
    assert_fleet_fanout(start)

    # Each enabled sub-service binds its own manifest entry, titled per sub-service, so
    # each carries a distinct tool prefix over the same managed server.
    alpha_title = f"e2e_noauth_multi_alpha_{alias}"
    beta_title = f"e2e_noauth_multi_beta_{alias}"
    assert set(start["added_manifest_entries"]) == {alpha_title, beta_title}
    alpha_tool = f"{alpha_title}_ping"
    beta_tool = f"{beta_title}_ping"

    connected = await converged_digest(stack, differ_from=baseline)
    titles = await _manifest_mcp_titles(stack)
    assert alpha_title in titles  # live-vs-persisted projection
    assert beta_title in titles  # live-vs-persisted projection
    await wait_present(
        lambda: _serves(stack, alpha_tool), deadline=30.0, message=f"managed tool {alpha_tool} never served fleet-wide"
    )
    await wait_present(
        lambda: _serves(stack, beta_tool), deadline=30.0, message=f"managed tool {beta_tool} never served fleet-wide"
    )

    # Toggle beta OFF (alpha stays enabled). Every worker converges away from the
    # both-on digest, beta's tool gone fleet-wide, alpha's still served.
    off = await api.request(
        "PATCH",
        f"/api/connectors/connections/{connection_id}/sub-services",
        json={"enabled_sub_services": ["alpha"]},
        retry_on_reloading=True,
    )
    assert_fleet_fanout(off)
    assert off["removed_manifest_entries"] == [beta_title]

    toggled_off = await converged_digest(stack, differ_from=connected)
    titles = await _manifest_mcp_titles(stack)
    assert beta_title not in titles  # live-vs-persisted projection
    assert alpha_title in titles  # live-vs-persisted projection
    await wait_present(
        lambda: _not_serves(stack, beta_tool),
        deadline=30.0,
        message=f"managed tool {beta_tool} still served after toggle-off",
    )
    assert await _serves(stack, alpha_tool), f"managed tool {alpha_tool} not served after toggle-off"

    # Toggle beta back ON. Every worker converges again, beta's tool restored fleet-wide.
    on = await api.request(
        "PATCH",
        f"/api/connectors/connections/{connection_id}/sub-services",
        json={"enabled_sub_services": ["alpha", "beta"]},
        retry_on_reloading=True,
    )
    assert_fleet_fanout(on)
    assert on["added_manifest_entries"] == [beta_title]

    await converged_digest(stack, differ_from=toggled_off)
    titles = await _manifest_mcp_titles(stack)
    assert beta_title in titles  # live-vs-persisted projection
    assert alpha_title in titles  # live-vs-persisted projection
    await wait_present(
        lambda: _serves(stack, beta_tool),
        deadline=30.0,
        message=f"managed tool {beta_tool} not restored after toggle-on",
    )
    assert await _serves(stack, alpha_tool), f"managed tool {alpha_tool} not served after toggle-on"


async def test_oauth_complete_and_reconnect_converges(
    fresh_stack: Callable[..., TaiStack], oauth_idp: OAuthIdp, uniq: Callable[[str], str]
) -> None:
    """An OAuth connect completed against the stub IdP binds ``e2e_idp``'s managed
    server fleet-wide; a reconnect (re-running the authorize flow) re-converges it. Both
    the oauth-complete and the reconnect ride the ``manifest_writer`` seam."""
    stack = _connectors_stack(fresh_stack, oauth_idp)
    api = stack.api()
    baseline = await converged_baseline(stack)

    alias = uniq("conn")
    start = await api.post(
        "/api/connectors/connections/start",
        json={"provider_id": "e2e_idp", "alias": alias, "enabled_sub_services": ["default"]},
        retry_on_reloading=True,
    )
    code, state = await _authorize(start["authorize_url"])
    completed = await api.post(
        "/api/connectors/oauth/complete", json={"code": code, "state": state}, retry_on_reloading=True
    )
    assert completed["kind"] == "success", f"oauth completion failed: {completed}"
    connection_id = completed["connection_id"]

    await converged_digest(stack, differ_from=baseline)
    entry_title = f"e2e_idp_default_{alias}"
    assert entry_title in await _manifest_mcp_titles(stack)  # live-vs-persisted projection
    tool = f"{entry_title}_ping"
    await wait_present(
        lambda: _serves(stack, tool), deadline=30.0, message=f"managed tool {tool} never served after oauth connect"
    )

    # Reconnect: re-run the authorize flow and complete again — the fleet re-converges.
    reconnect = await api.post(
        f"/api/connectors/connections/{connection_id}/reconnect",
        json={"enabled_sub_services": ["default"]},
        retry_on_reloading=True,
    )
    code2, state2 = await _authorize(reconnect["authorize_url"])
    completed2 = await api.post(
        "/api/connectors/oauth/complete", json={"code": code2, "state": state2}, retry_on_reloading=True
    )
    assert completed2["kind"] == "success", f"oauth reconnect completion failed: {completed2}"

    # Every worker converges on one digest, still serving the managed tool.
    await converged_digest(stack)
    assert await _serves(stack, tool), f"managed tool {tool} not served after reconnect"


async def _not_serves(stack: TaiStack, tool: str) -> bool:
    return not await _serves(stack, tool)
