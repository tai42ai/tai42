"""C-accounts — browser-less OIDC login against the in-process signing issuer.

Driving ``/api/login/oidc/<provider>/authorize`` and following the 302 chain through
the issuer lands a callback that mints a ``tai-sess-`` session; the SPA trades the
one-time SSO code for it and the session then authenticates a call. Login is
deny-by-default (an operator provisions the subject's policy). The callback rejects
a tampered ``state`` and an id_token that fails verification."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.netfixtures import OAuthIdp
from tai42_e2e.stack import TaiStack

# The pinned provider slug the oidc stack configures (see build_oidc_stack).
_PROVIDER = "e2e"


def _base(stack: TaiStack, port: int) -> str:
    return f"http://{stack.host}:{port}"


async def _follow_login_chain(stack: TaiStack) -> httpx.Response:
    """Drive authorize on replica B and follow the full 302 chain (issuer +
    callback), returning the final response — a cookie jar carries the flow-binding
    cookie across the authorize -> callback hop over loopback http."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        return await client.get(f"{_base(stack, stack.port_b)}/api/login/oidc/{_PROVIDER}/authorize")


async def test_oidc_login_mints_session_and_rejects_bad_flows(oidc_stack: TaiStack, oauth_idp: OAuthIdp) -> None:
    stack = oidc_stack
    admin = stack.api(port=stack.port_a)  # seeded root sk- key
    public_b = ApiClient(_base(stack, stack.port_b))

    # Deny-by-default: the callback provisions no policy, so an operator pre-provisions
    # one for the namespaced login subject before the session can reach a route.
    subject_id = f"oidc:{_PROVIDER}:{oauth_idp.subject}"
    await admin.post(
        "/api/auth/api-keys",
        json={"user_id": subject_id, "description": "oidc subject policy", "scopes": ["e2e-all"]},
    )

    # Happy path: authorize -> issuer -> callback mints a session -> SPA exchanges the
    # one-time SSO code for it.
    final = await _follow_login_chain(stack)
    sso_code = parse_qs(urlparse(str(final.url)).query).get("sso", [None])[0]
    assert sso_code, f"the login chain did not land on /login?sso=...: {final.url} ({final.status_code})"
    exchanged = await public_b.post("/api/login/sso/exchange", json={"code": sso_code})
    session = exchanged["token"]
    assert session.startswith("tai-sess-"), exchanged
    assert exchanged["user_id"] == subject_id, exchanged

    # The minted session authenticates a call on replica B.
    authed = stack.api(port=stack.port_b).with_token(session)
    kinds = await authed.get("/api/system/kinds")
    assert any(row["kind"] == "accounts" for row in kinds), kinds

    # Negative: a tampered state at the callback is rejected before any exchange.
    tampered = await public_b.request_raw(
        "GET", f"/api/login/oidc/{_PROVIDER}/callback?code=whatever&state=forged.deadbeef"
    )
    assert tampered.status_code == 400, f"a tampered state must 400: {tampered.status_code} {tampered.text}"

    # Negative: an id_token with a bad SIGNATURE (issuer signs with the off-JWKS key)
    # driven through the full authorize->callback flow is rejected at the callback.
    oauth_idp.set_sign_id_token_badly(True)
    try:
        bad_sig = await _follow_login_chain(stack)
    finally:
        oauth_idp.set_sign_id_token_badly(False)
    assert bad_sig.status_code == 401, (
        f"a bad-signature id_token must be rejected at the callback: {bad_sig.status_code} {bad_sig.text}"
    )

    # Negative: an id_token that fails verification for a different reason (issuer clock
    # shifted so the minted id_token is already expired) is also rejected at the callback.
    oauth_idp.set_clock_offset(-100_000)
    try:
        expired = await _follow_login_chain(stack)
    finally:
        oauth_idp.set_clock_offset(0)
    assert expired.status_code == 401, (
        f"an expired id_token must be rejected at the callback: {expired.status_code} {expired.text}"
    )
