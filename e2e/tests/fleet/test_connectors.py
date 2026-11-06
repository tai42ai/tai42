"""The connector manifest-writing paths converge the fleet.

Every connector mutation rides the same ``manifest_writer`` → ConfigService pipeline
that fans a ``reload_config`` out over the worker bus, so a connect / disconnect /
oauth-complete on ONE worker converges EVERY worker with NO manual reload. The proof is
the mutation's own fan-out report: every serve member on the bus census at the time of
the call has an ``applied`` entry and no entry is anything else. The served surface is
the caller's read of the change: the REAL managed MCP server
(``tai42_e2e_fixtures.managed_mcp_server``, launched by each fixture provider's
sub-service) has its ``ping`` tool served after a connect and not served after a
disconnect.

* connect a no-auth provider → every serve member applies it and the managed tool is
  served; disconnect → every serve member applies it and the tool is not served.
* PATCH the two-sub-service ``e2e_noauth_multi`` provider to toggle ONE sub-service
  off (leaving the other enabled) → every serve member applies it and just that
  sub-service's tool is not served; toggle it back on → it is served again.
* OAuth-complete + reconnect via ``e2e_idp`` (whose sub-service rides the same
  launchable server) → every serve member applies each completion.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tai42_e2e.oauth_idp import OAuthIdp
from tai42_e2e.stack import TaiStack
from tai42_e2e_fixtures.managed_mcp_server import PING_ANNOTATIONS, PING_TITLE

from ._fleet import (
    assert_applied_by,
    assert_fleet_fanout,
    build_fleet_connectors_stack,
    connectors_resource_kwargs,
    serve_members,
    wait_present,
)

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs(
        "kind:connectors",
        "probe-tools",
        "mutable",
        "process",
        "topology:multiworker",
        "setting:managed-mcp-server",
        "setting:manifest:connectors-fixtures",
        "setting:TAI_MCP_MCP_RELOAD_PROBE_TIMEOUT=15",
    ),
]


async def _manifest_mcp_titles(stack: TaiStack) -> list[str]:
    manifest = await stack.api().get("/api/manifest", retry_on_reloading=True)
    return [entry["title"] for entry in manifest["mcp"]]


async def _serves(stack: TaiStack, tool: str) -> bool:
    async with stack.mcp() as mcp:
        return tool in await mcp.tool_names()


async def _authorize(authorize_url: str) -> tuple[str, str]:
    """Follow the authorize URL to the stub IdP and recover (code, state) from its
    redirect back to the connector callback."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
        response = await client.get(authorize_url)
    query = parse_qs(urlparse(response.headers["location"]).query)
    return query["code"][0], query["state"][0]


def _connectors_stack(fresh_stack: Callable[..., TaiStack], oauth_idp: OAuthIdp) -> TaiStack:
    return fresh_stack(build_fleet_connectors_stack, resource_kwargs=connectors_resource_kwargs(oauth_idp.base_url))


# A liveness bound (a bound on a hang, not a performance limit): twice the slowest legitimate
# run, stack boot included, measured on CI (arq, shard 2, 2026-11-06: 26.5s), rounded up to the
# next 10s.
@pytest.mark.timeout(60)
async def test_noauth_connect_disconnect_converges(
    fresh_stack: Callable[..., TaiStack], oauth_idp: OAuthIdp, uniq: Callable[[str], str]
) -> None:
    """Connecting the no-auth ``e2e_noauth_alpha`` provider is applied by EVERY serve member
    with no manual reload and its managed server's tool is served; disconnecting is applied by
    every serve member and the tool is not served (the stale-serving regression). The
    connector-written mcp entry is served / not served in ``GET /api/manifest`` as the
    persisted doc says."""
    stack = _connectors_stack(fresh_stack, oauth_idp)
    api = stack.api()

    alias = uniq("conn")
    members = serve_members(stack)
    start = await api.post(
        "/api/connectors/connections/start",
        json={"provider_id": "e2e_noauth_alpha", "alias": alias, "enabled_sub_services": ["default"]},
        retry_on_reloading=True,
    )
    connection_id = start["connection_id"]
    entry_title = start["added_manifest_entries"][0]
    assert_applied_by(assert_fleet_fanout(start), members)

    assert entry_title in await _manifest_mcp_titles(stack)  # live-vs-persisted projection
    tool = f"{entry_title}_ping"
    await wait_present(lambda: _serves(stack, tool), deadline=30.0, message=f"managed tool {tool} not served")
    # The bound tool is re-served with the display title and hints its server advertises.
    async with stack.mcp() as mcp:
        served = next(listed for listed in await mcp.list_tools() if listed.name == tool)
    assert (served.title, served.annotations) == (PING_TITLE, PING_ANNOTATIONS), served

    # Disconnect → every serve member drops the manifest entry and its tools.
    members = serve_members(stack)
    disconnect = await api.delete(f"/api/connectors/connections/{connection_id}", retry_on_reloading=True)
    assert_applied_by(assert_fleet_fanout(disconnect), members)

    assert entry_title not in await _manifest_mcp_titles(stack)  # live-vs-persisted projection
    await wait_present(
        lambda: _not_serves(stack, tool), deadline=30.0, message=f"managed tool {tool} still served after disconnect"
    )


# A liveness bound (a bound on a hang, not a performance limit): twice the slowest legitimate
# run, stack boot included, measured on CI (arq, shard 8, 2026-11-06: 35.7s), rounded up to the
# next 10s.
@pytest.mark.timeout(80)
async def test_sub_service_toggle_converges(
    fresh_stack: Callable[..., TaiStack], oauth_idp: OAuthIdp, uniq: Callable[[str], str]
) -> None:
    """Connecting the two-sub-service no-auth ``e2e_noauth_multi`` provider with
    BOTH sub-services is applied by EVERY serve member, and each one's managed tool (each
    under its own manifest-entry prefix) is served. A PATCH toggling ONE sub-service off —
    leaving the other enabled, so the ``min_length=1`` floor holds — is applied by every serve
    member and just that sub-service's tool is not served; toggling it back on is applied by
    every serve member and the tool is served again. Each direction rides the
    ``manifest_writer`` seam and converges the fleet with no manual reload."""
    stack = _connectors_stack(fresh_stack, oauth_idp)
    api = stack.api()

    alias = uniq("conn")
    members = serve_members(stack)
    start = await api.post(
        "/api/connectors/connections/start",
        json={"provider_id": "e2e_noauth_multi", "alias": alias, "enabled_sub_services": ["alpha", "beta"]},
        retry_on_reloading=True,
    )
    connection_id = start["connection_id"]
    assert_applied_by(assert_fleet_fanout(start), members)

    # Each enabled sub-service binds its own manifest entry, titled per sub-service, so
    # each carries a distinct tool prefix over the same managed server.
    alpha_title = f"e2e_noauth_multi_alpha_{alias}"
    beta_title = f"e2e_noauth_multi_beta_{alias}"
    assert set(start["added_manifest_entries"]) == {alpha_title, beta_title}
    alpha_tool = f"{alpha_title}_ping"
    beta_tool = f"{beta_title}_ping"

    titles = await _manifest_mcp_titles(stack)
    assert alpha_title in titles  # live-vs-persisted projection
    assert beta_title in titles  # live-vs-persisted projection
    await wait_present(
        lambda: _serves(stack, alpha_tool), deadline=30.0, message=f"managed tool {alpha_tool} not served"
    )
    await wait_present(lambda: _serves(stack, beta_tool), deadline=30.0, message=f"managed tool {beta_tool} not served")

    # Toggle beta OFF (alpha stays enabled). Every serve member applies it: beta's tool is
    # not served, alpha's still is.
    members = serve_members(stack)
    off = await api.request(
        "PATCH",
        f"/api/connectors/connections/{connection_id}/sub-services",
        json={"enabled_sub_services": ["alpha"]},
        retry_on_reloading=True,
    )
    assert_applied_by(assert_fleet_fanout(off), members)
    assert off["removed_manifest_entries"] == [beta_title]

    titles = await _manifest_mcp_titles(stack)
    assert beta_title not in titles  # live-vs-persisted projection
    assert alpha_title in titles  # live-vs-persisted projection
    await wait_present(
        lambda: _not_serves(stack, beta_tool),
        deadline=30.0,
        message=f"managed tool {beta_tool} still served after toggle-off",
    )
    assert await _serves(stack, alpha_tool), f"managed tool {alpha_tool} not served after toggle-off"

    # Toggle beta back ON. Every serve member applies it and beta's tool is served again.
    members = serve_members(stack)
    on = await api.request(
        "PATCH",
        f"/api/connectors/connections/{connection_id}/sub-services",
        json={"enabled_sub_services": ["alpha", "beta"]},
        retry_on_reloading=True,
    )
    assert_applied_by(assert_fleet_fanout(on), members)
    assert on["added_manifest_entries"] == [beta_title]

    titles = await _manifest_mcp_titles(stack)
    assert beta_title in titles  # live-vs-persisted projection
    assert alpha_title in titles  # live-vs-persisted projection
    await wait_present(
        lambda: _serves(stack, beta_tool),
        deadline=30.0,
        message=f"managed tool {beta_tool} not restored after toggle-on",
    )
    assert await _serves(stack, alpha_tool), f"managed tool {alpha_tool} not served after toggle-on"


# A liveness bound (a bound on a hang, not a performance limit): twice the slowest legitimate
# run, stack boot included, measured on CI (arq, shard 7, 2026-11-06: 21.2s), rounded up to the
# next 10s.
@pytest.mark.timeout(50)
@pytest.mark.needs("helper:idp")
async def test_oauth_complete_and_reconnect_converges(
    fresh_stack: Callable[..., TaiStack], oauth_idp: OAuthIdp, uniq: Callable[[str], str]
) -> None:
    """An OAuth connect completed against the stub IdP is applied by every serve member and
    ``e2e_idp``'s managed tool is served. A reconnect re-runs the authorize flow with the same
    sub-services: its completion succeeds and the tool is still served, with no fleet
    mutation, because the manifest entries it would write are already persisted."""
    stack = _connectors_stack(fresh_stack, oauth_idp)
    api = stack.api()

    alias = uniq("conn")
    start = await api.post(
        "/api/connectors/connections/start",
        json={"provider_id": "e2e_idp", "alias": alias, "enabled_sub_services": ["default"]},
        retry_on_reloading=True,
    )
    code, state = await _authorize(start["authorize_url"])
    members = serve_members(stack)
    completed = await api.post(
        "/api/connectors/oauth/complete", json={"code": code, "state": state}, retry_on_reloading=True
    )
    assert completed["kind"] == "success", f"oauth completion failed: {completed}"
    connection_id = completed["connection_id"]
    assert_applied_by(assert_fleet_fanout(completed), members)

    entry_title = f"e2e_idp_default_{alias}"
    assert entry_title in await _manifest_mcp_titles(stack)  # live-vs-persisted projection
    tool = f"{entry_title}_ping"
    await wait_present(
        lambda: _serves(stack, tool), deadline=30.0, message=f"managed tool {tool} never served after oauth connect"
    )

    # Reconnect: re-run the authorize flow and complete again; the manifest is unchanged.
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
    assert await _serves(stack, tool), f"managed tool {tool} not served after reconnect"


async def _not_serves(stack: TaiStack, tool: str) -> bool:
    return not await _serves(stack, tool)
