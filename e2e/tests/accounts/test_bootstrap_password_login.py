"""C-accounts — first-owner bootstrap and password login across replicas.

The accounts provider resolves ``tai-sess-`` sessions minted on one replica on
any other, so a session minted from a password login on A authenticates a call
on B. Bootstrap is a one-shot gated create: two concurrent create-owner requests
race the plugin's advisory lock and exactly one wins."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

_PASSWORD = "correct-horse-battery-staple"


def _unauth(stack: TaiStack, port: int) -> ApiClient:
    """A client for one replica carrying NO credential (the public login surface
    and the unauthenticated-negative probes)."""
    return ApiClient(f"http://{stack.host}:{port}")


async def test_bootstrap_and_password_login(accounts_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    stack = accounts_stack
    token = stack.config.env["TAI_ACCOUNTS_BOOTSTRAP_TOKEN"]
    public_a = _unauth(stack, stack.port_a)
    public_b = _unauth(stack, stack.port_b)

    # /api/login/methods is PUBLIC (the always-public /api/login prefix), reports the
    # gated create-owner posture, and lives under /api/login — never /api/auth.
    methods = await public_b.get("/api/login/methods")
    assert methods["bootstrap"] is True, f"a fresh deployment must report bootstrap: {methods}"
    ids = {m["id"]: m for m in methods["methods"]}
    assert ids["password"]["submit_path"] == "/api/login/password", methods
    assert ids["bootstrap"]["submit_path"] == "/api/login/bootstrap", methods
    # The methods door lives ONLY under the public /api/login prefix — the same suffix
    # under the reserved /api/auth namespace does not serve methods; it is swept into
    # the authed namespace and denied (401), never answered public.
    leaked = await public_b.request_raw("GET", "/api/auth/login/methods")
    assert leaked.status_code == 401, f"login methods must not answer public under /api/auth: {leaked.status_code}"
    # The reserved /api/auth namespace is not public: an unauthenticated user list is denied.
    unauth_users = await public_b.request_raw("GET", "/api/auth/users")
    assert unauth_users.status_code == 401, f"/api/auth must be authed, got {unauth_users.status_code}"

    # Two simultaneous create-owner requests (one per replica) race the plugin's
    # advisory lock: exactly one creates the owner, the other gets a 4xx.
    email_a = f"{uniq('owner')}@e2e.test"
    email_b = f"{uniq('owner')}@e2e.test"
    body_a = {"email": email_a, "password": _PASSWORD, "bootstrap_token": token}
    body_b = {"email": email_b, "password": _PASSWORD, "bootstrap_token": token}
    res_a, res_b = await asyncio.gather(
        public_a.request_raw("POST", "/api/login/bootstrap", json=body_a),
        public_b.request_raw("POST", "/api/login/bootstrap", json=body_b),
    )
    statuses = sorted([res_a.status_code, res_b.status_code])
    assert statuses == [200, 409], f"exactly one bootstrap must win: {statuses} ({res_a.text} | {res_b.text})"
    winner_email = email_a if res_a.status_code == 200 else email_b

    # The owner now exists, so the create-owner posture is gone.
    methods_after = await public_a.get("/api/login/methods")
    assert methods_after["bootstrap"] is False, f"bootstrap must be off once the owner exists: {methods_after}"

    # A sequential second bootstrap is refused (owner already initialized).
    second = await public_a.request_raw(
        "POST",
        "/api/login/bootstrap",
        json={"email": f"{uniq('owner')}@e2e.test", "password": _PASSWORD, "bootstrap_token": token},
    )
    assert second.status_code == 409, f"a second bootstrap must 4xx: {second.status_code} {second.text}"

    # Password login on A mints a tai-sess- session; it authenticates a call on B.
    login = await public_a.post("/api/login/password", json={"email": winner_email, "password": _PASSWORD})
    session = login["token"]
    assert session.startswith("tai-sess-"), f"login must mint a session token: {session[:12]!r}"
    authed_b = ApiClient(f"http://{stack.host}:{stack.port_b}").with_token(session)
    kinds = await authed_b.get("/api/system/kinds")
    assert any(row["kind"] == "accounts" for row in kinds), f"the session must reach an authed route on B: {kinds}"

    # A wrong password is a uniform 401 (never a leak of which field was wrong).
    wrong = await public_a.request_raw(
        "POST", "/api/login/password", json={"email": winner_email, "password": "wrong-password-value"}
    )
    assert wrong.status_code == 401, f"a wrong password must 401: {wrong.status_code} {wrong.text}"
