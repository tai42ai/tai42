"""C-accounts — a viewer keeps its self-service writes.

A route declared self-service is not governed by a role's per-tag level: a viewer
session sees the key-mint route in its own projection, mints a key of its own and
logs itself out, though its grant map gives it no write on any tag."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.accounts_flow import invite_accept_login
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

pytestmark = pytest.mark.needs("kind:identity", "kind:accounts:postgres", "mutable", "setting:seeded-access-control")

# The one non-public scope the accounts stack maps every /api route onto (see the
# seeded route table): a real, mintable scope the viewer's own key can carry.
_SCOPE = "e2e-all"
_PASSWORD = "viewer-user-password-1"


async def test_viewer_session_logs_out_and_mints_its_own_key(
    accounts_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)  # seeded root sk- key (unconditional "*" admin)
    _, session = await invite_accept_login(
        admin,
        ApiClient(stack.origin(stack.port_a)),
        email=f"{uniq('viewer')}@e2e.test",
        role="viewer",
        password=_PASSWORD,
    )
    viewer = stack.api(port=stack.port_a).with_token(session)

    # The viewer's own projection lists the key-mint route with its POST method.
    me = await viewer.get("/api/auth/me")
    mint_routes = [row for row in me["routes"] if row["path"] == "/api/auth/api-keys"]
    assert any("POST" in row["methods"] for row in mint_routes), me["routes"]

    # The viewer mints a key of its own.
    minted = await viewer.post(
        "/api/auth/api-keys",
        json={"user_id": uniq("viewer-key"), "description": "viewer's own key", "scopes": [_SCOPE]},
    )
    assert minted["api_key"].startswith("sk-"), minted

    # The viewer logs itself out, and the session no longer authenticates.
    revoked = await viewer.post("/api/auth/logout")
    assert revoked["revoked"] is True, revoked

    async def session_dead() -> bool:
        response = await viewer.request_raw("GET", "/api/auth/me")
        return response.status_code == 401

    await wait_for_async(session_dead, deadline=10.0, message="the viewer session still authenticated after logout")
