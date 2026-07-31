"""C-accounts — the admin-invite lifecycle across replicas.

An admin mints a user with a one-time invite on replica A; the invitee accepts it
(setting a first password) and the resulting account logs in on replica B. The
invite is single-use — the same atomic consume that enforces its TTL rejects a
replayed or unknown token — and the invited non-admin cannot reach the reserved
user-administration surface."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack

_PASSWORD = "invited-user-password-1"


def _unauth(stack: TaiStack, port: int) -> ApiClient:
    return ApiClient(f"http://{stack.host}:{port}")


async def test_invite_accept_login_and_rejections(accounts_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    stack = accounts_stack
    admin = stack.api(port=stack.port_a)  # the seeded root sk- key (an unconditional "*" admin)
    public_a = _unauth(stack, stack.port_a)
    public_b = _unauth(stack, stack.port_b)

    email = f"{uniq('invitee')}@e2e.test"
    invite = await admin.post("/api/auth/users", json={"email": email, "role": "editor"})
    invite_token = invite["invite_token"]
    assert invite["login_path"].startswith("/login?invite="), invite

    # Accepting the invite sets the first password and mints a session immediately.
    accepted = await public_a.post(
        "/api/login/invite/accept",
        json={"invite_token": invite_token, "password": _PASSWORD, "password_confirm": _PASSWORD},
    )
    session = accepted["token"]
    assert session.startswith("tai-sess-"), accepted

    # The account now has a password: a normal login on replica B succeeds.
    login_b = await public_b.post("/api/login/password", json={"email": email, "password": _PASSWORD})
    assert login_b["token"].startswith("tai-sess-"), login_b

    # The invite is single-use: replaying the consumed token is rejected (the same
    # atomic-consume miss branch that also rejects an expired invite).
    replay = await public_a.request_raw(
        "POST",
        "/api/login/invite/accept",
        json={"invite_token": invite_token, "password": _PASSWORD, "password_confirm": _PASSWORD},
    )
    assert replay.status_code == 400, f"a replayed invite must 400: {replay.status_code} {replay.text}"

    # An unknown invite token hits the same miss branch (unknown / expired / consumed
    # are indistinguishable to the caller).
    unknown = await public_a.request_raw(
        "POST",
        "/api/login/invite/accept",
        json={"invite_token": "tai-inv-does-not-exist", "password": _PASSWORD, "password_confirm": _PASSWORD},
    )
    assert unknown.status_code == 400, f"an unknown invite must 400: {unknown.status_code} {unknown.text}"

    # The invited editor is a NON-admin: its jq condition fences it out of the reserved
    # user-administration surface, so it cannot create (mutate) users.
    editor = stack.api(port=stack.port_b).with_token(session)
    denied = await editor.request_raw(
        "POST", "/api/auth/users", json={"email": f"{uniq('x')}@e2e.test", "role": "viewer"}
    )
    assert denied.status_code == 403, f"an invited editor must not mutate /api/auth/users: {denied.status_code}"
