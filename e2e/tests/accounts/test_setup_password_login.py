"""C-accounts — the setup door attaches the owner's login, and invites create principals.

On a fresh accounts deployment the setup door can attach an interactive login: it reports
what it accepts (``setup_login.kinds``), initializes with a password credential, and the
owner then logs in for a session. Once the owner exists the door is shut (409), and the
admin brings further humans in through invites — each an invite that creates a principal.
"""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.accounts_flow import invite_accept_login
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.manifests import _SETUP_TOKEN
from tai42_e2e.stack import TaiStack

_PASSWORD = "correct-horse-battery-staple"


def _unauth(stack: TaiStack) -> ApiClient:
    """A client carrying NO credential — the public login surface and unauth-negative probes."""
    return ApiClient(f"http://{stack.host}:{stack.port_a}")


async def test_setup_attaches_password_login_and_invites_create_principals(
    accounts_fresh_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = accounts_fresh_stack
    public = _unauth(stack)

    # /api/login/methods is PUBLIC and reports the fresh posture: setup is needed and the
    # configured accounts provider advertises exactly a password and an invite login.
    methods = await public.get("/api/login/methods")
    assert methods["needs_setup"] is True, methods
    assert methods["setup_login"]["kinds"] == ["password", "invite"], methods
    ids = {m["id"]: m for m in methods["methods"]}
    assert set(ids) == {"password", "invite"}, methods
    assert ids["password"]["submit_path"] == "/api/login/password", methods

    # The methods door lives ONLY under the public /api/login prefix; the same suffix under
    # the reserved /api/auth namespace is denied, and the user list is authed.
    leaked = await public.request_raw("GET", "/api/auth/login/methods")
    assert leaked.status_code in (401, 403), (
        f"login methods must not answer public under /api/auth: {leaked.status_code}"
    )
    unauth_users = await public.request_raw("GET", "/api/auth/users")
    assert unauth_users.status_code in (401, 403), f"/api/auth must be authed, got {unauth_users.status_code}"

    # Setup initializes the deployment AND attaches the owner's password login in one call.
    owner_email = f"{uniq('owner')}@e2e.test"
    res = await public.request_raw(
        "POST",
        "/api/setup",
        json={
            "setup_token": _SETUP_TOKEN,
            "owner_user_id": "owner",
            "owner_display_name": "Owner",
            "login": {"kind": "password", "email": owner_email, "password": _PASSWORD},
        },
    )
    assert res.status_code == 200, f"setup with a password login must 200: {res.status_code} {res.text}"
    initialized = res.json()["data"]
    assert initialized["login_attached"] is True, initialized

    # Password login mints a tai-sess- session that authenticates an authed route; the owner
    # projects as a human principal.
    login = await public.post("/api/login/password", json={"email": owner_email, "password": _PASSWORD})
    session = login["token"]
    assert session.startswith("tai-sess-"), f"login must mint a session token: {session[:12]!r}"
    owner = ApiClient(f"http://{stack.host}:{stack.port_a}").with_token(session)
    me = await owner.get("/api/auth/me")
    assert me["principal"]["kind"] == "human", me

    # One-shot: needs_setup is now false and a second setup is refused.
    methods_after = await public.get("/api/login/methods")
    assert methods_after["needs_setup"] is False, methods_after
    second = await public.request_raw(
        "POST",
        "/api/setup",
        json={"setup_token": _SETUP_TOKEN, "owner_user_id": "owner-2", "owner_display_name": "Owner 2"},
    )
    assert second.status_code == 409, f"a second setup must 409: {second.status_code} {second.text}"

    # A wrong password is a uniform 401.
    wrong = await public.request_raw(
        "POST", "/api/login/password", json={"email": owner_email, "password": "wrong-password-value"}
    )
    assert wrong.status_code == 401, f"a wrong password must 401: {wrong.status_code} {wrong.text}"

    # Invites create principals: the owner admin brings a user in through invite/accept/login,
    # and that user shows up as a principal created by the owner.
    invited_email = f"{uniq('invitee')}@e2e.test"
    invited_id, _ = await invite_accept_login(owner, public, email=invited_email, role="editor", password=_PASSWORD)
    principals = await owner.get("/api/auth/principals")
    invited_principal = next((p for p in principals if p["user_id"] == invited_id), None)
    assert invited_principal is not None, principals
    assert invited_principal["created_by"] == "owner", invited_principal
