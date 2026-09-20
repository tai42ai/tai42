"""C-accounts — ordered multi-provider credential resolution on one deployment.

The same stack resolves an ``sk-`` API key (the redis provider) AND a ``tai-sess-``
session (the accounts provider): a credential each provider claims is answered by
its owner, a credential no provider claims is a clean 401, and a session presented
after logout no longer authenticates."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for_async

_PASSWORD = "chain-user-password-1"


async def test_key_and_session_both_resolve(accounts_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)  # seeded root sk- key (the redis provider's principal)

    # A session credential from the accounts provider, minted via an accepted invite.
    created = await admin.post("/api/auth/users", json={"email": f"{uniq('user')}@e2e.test", "role": "editor"})
    public = ApiClient(f"http://{stack.host}:{stack.port_a}")
    accepted = await public.post(
        "/api/login/invite/accept",
        json={"invite_token": created["invite_token"], "password": _PASSWORD, "password_confirm": _PASSWORD},
    )
    session = accepted["token"]
    assert session.startswith("tai-sess-"), accepted

    # Both credential kinds resolve against replica B: the sk- key (redis provider)
    # and the tai-sess- session (accounts provider).
    key_ok = await stack.api(port=stack.port_b).get("/api/system/kinds")
    assert any(row["kind"] == "accounts" for row in key_ok), key_ok
    session_b = stack.api(port=stack.port_b).with_token(session)
    session_ok = await session_b.get("/api/system/kinds")
    assert any(row["kind"] == "accounts" for row in session_ok), session_ok

    # A credential no provider claims is a clean 401 (never an error), for both shapes.
    for garbage in (f"sk-{uniq('nope')}", f"tai-sess-{uniq('nope')}"):
        denied = (
            await ApiClient(f"http://{stack.host}:{stack.port_b}")
            .with_token(garbage)
            .request_raw("GET", "/api/system/kinds")
        )
        assert denied.status_code == 401, f"garbage token {garbage[:10]!r} must 401, got {denied.status_code}"

    # After logout on A, the session no longer authenticates on B.
    revoked = await session_b.post("/api/auth/logout")
    assert revoked["revoked"] is True, revoked

    async def session_dead() -> bool:
        response = await session_b.request_raw("GET", "/api/system/kinds")
        return response.status_code == 401

    await wait_for_async(session_dead, deadline=10.0, message="the session still authenticated after logout")
