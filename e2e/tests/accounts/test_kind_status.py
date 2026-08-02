"""C-accounts — the authed pluggable-kind status door.

``GET /api/system/kinds`` reports the live registry state of every pluggable kind.
On the accounts stack the identity row reflects the configured ``auth_providers``
resolution chain and the accounts row is active; the route names installed plugins,
so it is authed and never public."""

from __future__ import annotations

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack


async def test_kind_status_authed_and_reflects_providers(accounts_stack: TaiStack) -> None:
    stack = accounts_stack

    rows = await stack.api(port=stack.port_b).get("/api/system/kinds")
    by_kind = {row["kind"]: row for row in rows}

    identity = by_kind["identity"]
    assert identity["state"] == "active", identity
    # The identity row names the configured ordered auth_providers chain.
    assert "accounts-postgres" in identity["plugin"], identity
    assert "redis" in identity["plugin"], identity

    accounts = by_kind["accounts"]
    assert accounts["state"] == "active", accounts
    assert "accounts-postgres" in accounts["plugin"], accounts

    # The route is NOT public: an unauthenticated call is denied.
    unauth = await ApiClient(f"http://{stack.host}:{stack.port_b}").request_raw("GET", "/api/system/kinds")
    assert unauth.status_code == 401, f"/api/system/kinds must be authed, got {unauth.status_code}"


async def test_connectors_live_via_store_resolution(accounts_stack: TaiStack) -> None:
    # Connectors gates on its store config like every other DB feature. This stack sets
    # no explicit CONNECTOR_STORE_* env, yet the shared TAI_DEFAULT_PG_PASSWORD resolves
    # the store — so connectors is live purely by store resolution, no intent marker.
    # Pair the two windows on that fact: kinds reports the connectors row active and
    # /ready pings the connector store clean.
    stack = accounts_stack

    rows = await stack.api(port=stack.port_b).get("/api/system/kinds")
    connectors = next(row for row in rows if row["kind"] == "connectors")
    assert connectors["state"] == "active", connectors

    ready = await ApiClient(f"http://{stack.host}:{stack.port_b}").request_raw("GET", "/ready")
    assert ready.status_code == 200, f"/ready not ready: {ready.status_code} {ready.text}"
    assert ready.json()["checks"].get("connectors") == "ok", ready.text
