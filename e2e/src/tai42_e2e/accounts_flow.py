"""The accounts sign-in idiom shared across the suites: bring a human principal in through
the setup-door's successors — an admin invite, an anonymous accept that sets the password,
and a password login for the session the caller then acts with."""

from __future__ import annotations

from tai42_e2e.httpapi import ApiClient


async def invite_accept_login(
    api_admin: ApiClient,
    api_anon: ApiClient,
    *,
    email: str,
    role: str,
    password: str,
    retry_on_reloading: bool = False,
) -> tuple[str, str]:
    """Provision a ``role`` human and log it in; return ``(user_id, session_token)``.

    ``api_admin`` (an admin credential) mints the principal and its one-time invite;
    ``api_anon`` (no credential) accepts the invite — setting ``password`` and minting the
    principal's first ``tai-sess-`` session — and then logs in with that password for a
    fresh session the caller acts with. ``retry_on_reloading`` threads through to each call
    so a caller invoking this in a boot/reload window rides the reload gate.
    """
    invite = await api_admin.post(
        "/api/auth/users", json={"email": email, "role": role}, retry_on_reloading=retry_on_reloading
    )
    accepted = await api_anon.post(
        "/api/login/invite/accept",
        json={"invite_token": invite["invite_token"], "password": password, "password_confirm": password},
        retry_on_reloading=retry_on_reloading,
    )
    assert accepted["token"].startswith("tai-sess-"), f"invite accept must mint a session: {accepted}"
    login = await api_anon.post(
        "/api/login/password", json={"email": email, "password": password}, retry_on_reloading=retry_on_reloading
    )
    assert login["token"].startswith("tai-sess-"), f"login must mint a session token: {login}"
    return invite["user_id"], login["token"]
