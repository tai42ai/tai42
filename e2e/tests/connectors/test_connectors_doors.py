"""Connector door hardening.

A syntactically malformed connection id (not a UUID) keys no record, so the read and
delete doors answer a plain 404 — never a 500 that would leak the store's shape as an
oracle. And a request-body validation failure returns the failing field PATHS with the
submitted VALUES stripped: a rejected ``config_values`` secret must never ride the
400 body. A tool pick written on a connection's own manifest entry through the
tool-filter write survives a reconnect of that connection.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

pytestmark = pytest.mark.needs("kind:connectors")

# Not a UUID — the store rejects it before any lookup, so it is indistinguishable from a
# genuine miss (both 404).
_MALFORMED_ID = "not-a-valid-uuid"


async def test_malformed_connection_id_is_404_on_read_and_delete(connectors_stack: TaiStack) -> None:
    api = connectors_stack.api(port=connectors_stack.port_a)

    read = await api.request_raw("GET", f"/api/connectors/connections/{_MALFORMED_ID}")
    assert read.status_code == 404, read.text
    assert read.json()["error"] == "connection not found"

    delete = await api.request_raw("DELETE", f"/api/connectors/connections/{_MALFORMED_ID}")
    assert delete.status_code == 404, delete.text
    assert delete.json()["error"] == "connection not found"


async def test_start_connect_validation_body_carries_field_paths_not_values(connectors_stack: TaiStack) -> None:
    api = connectors_stack.api(port=connectors_stack.port_a)
    # A rejected config value shaped like a secret; it must NOT appear anywhere in the body.
    secret = "SENTINEL-SECRET-VALUE-9f3ab2"

    resp = await api.request_raw(
        "POST",
        "/api/connectors/connections/start",
        # ``config_values`` is dict[str, str]; a list value fails validation on that field.
        json={
            "provider_id": "e2e_idp",
            "alias": "x",
            "enabled_sub_services": ["default"],
            "config_values": {"client_secret": [secret]},
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"] == "invalid request body", body
    fields = body["fields"]
    assert isinstance(fields, list), body
    assert fields, body
    # Every entry names WHICH field failed and HOW — a dotted loc + a pydantic error type,
    # nothing else (no ``input``/``ctx`` that could carry the value).
    assert all(set(entry) == {"loc", "type"} for entry in fields), fields
    assert any("config_values" in entry["loc"] and "client_secret" in entry["loc"] for entry in fields), fields
    # The submitted value is nowhere in the response.
    assert secret not in resp.text, resp.text


# How long a manifest write on one replica may take to be bound on the other.
_REBIND_S = 60.0


async def _oauth_complete(api: ApiClient, authorize_url: str) -> dict:
    """Follow ``authorize_url`` to the stub IdP and complete the flow with its code and state."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
        response = await client.get(authorize_url)
    query = parse_qs(urlparse(response.headers["location"]).query)
    completed = await api.post(
        "/api/connectors/oauth/complete", json={"code": query["code"][0], "state": query["state"][0]}
    )
    assert completed["kind"] == "success", completed
    return completed


async def _entry(api: ApiClient, title: str) -> dict:
    manifest = await api.get("/api/manifest", retry_on_reloading=True)
    return next(item for item in manifest["mcp"] if item["title"] == title)


@pytest.mark.needs(
    "mutable",
    "helper:idp",
    "topology:replicas",
    "setting:manifest:connectors=e2e_idp",
    "setting:E2E_IDP_CLIENT_ID=e2e-client",
    "setting:CONNECTORS_REDIRECT_URI_ALLOWLIST=stack-origins",
    "setting:managed-mcp-server",
)
@pytest.mark.timeout(300)
async def test_a_pick_on_a_connections_entry_survives_a_reconnect(
    connectors_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = connectors_stack.api(port=connectors_stack.port_a)
    api_b = connectors_stack.api(port=connectors_stack.port_b)
    alias = uniq("pick")
    title = f"e2e_idp_default_{alias}"
    unpicked = f"{title}_reflect_env"
    kept = {f"{title}_ping", f"{title}_echo"}

    started = await api_a.post(
        "/api/connectors/connections/start",
        json={"provider_id": "e2e_idp", "alias": alias, "enabled_sub_services": ["default"]},
        retry_on_reloading=True,
    )
    connection_id = (await _oauth_complete(api_b, started["authorize_url"]))["connection_id"]

    async def bound_on_both(present: set[str], absent: set[str]) -> None:
        for api in (api_a, api_b):

            async def settled(api: ApiClient = api) -> bool:
                names = set(await api.get("/api/tools", retry_on_reloading=True))
                return present <= names and not (absent & names)

            await wait_for_async(settled, deadline=_REBIND_S, message=f"tools never read {present} without {absent}")

    await bound_on_both(kept | {unpicked}, set())

    await api_a.put(
        f"/api/mcp-config/entries/{title}/tool-filter",
        json={"include": [], "exclude": [unpicked]},
        retry_on_reloading=True,
    )
    await bound_on_both(kept, {unpicked})

    restarted = await api_a.post(
        f"/api/connectors/connections/{connection_id}/reconnect",
        json={"enabled_sub_services": ["default"]},
        retry_on_reloading=True,
    )
    await _oauth_complete(api_b, restarted["authorize_url"])

    for api in (api_a, api_b):
        entry = await _entry(api, title)
        assert (entry.get("include", []), entry.get("exclude", [])) == ([], [unpicked]), entry
        assert entry["managed"]["connection_id"] == connection_id, entry
    await bound_on_both(kept, {unpicked})

    await api_a.delete(f"/api/connectors/connections/{connection_id}", retry_on_reloading=True)
