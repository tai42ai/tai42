"""C-accounts — validate-only issuer JWTs, namespaced and deny-by-default.

The identity-oidc provider resolves an issuer-minted JWT to a namespaced
``idp:{issuer}:{sub}`` principal, verified against the issuer's JWKS. An operator
pre-provisions policy under that namespaced id; a policy under the BARE subject does
NOT apply (the namespacing fences issuer subjects off from other principals), an
unknown subject is denied, and a JWT that fails verification (expired or wrong
signature) never authenticates."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.netfixtures import OAuthIdp
from tai42_e2e.stack import TaiStack


def _base(stack: TaiStack, port: int) -> str:
    return f"http://{stack.host}:{port}"


async def test_issuer_jwt_namespaced_and_deny_by_default(
    oidc_stack: TaiStack, oauth_idp: OAuthIdp, uniq: Callable[[str], str]
) -> None:
    stack = oidc_stack
    admin = stack.api(port=stack.port_a)  # seeded root sk- key
    audience = stack.config.env["TAI_IDENTITY_OIDC_AUDIENCE"]
    issuer = oauth_idp.base_url

    def client(token: str) -> ApiClient:
        return ApiClient(_base(stack, stack.port_b)).with_token(token)

    # A subject whose NAMESPACED policy is provisioned — the issuer JWT for it reaches
    # a protected route on replica B.
    granted_sub = uniq("robot")
    await admin.post(
        "/api/auth/api-keys",
        json={"user_id": f"idp:{issuer}:{granted_sub}", "description": "issuer subject policy", "scopes": ["e2e-all"]},
    )
    granted = await client(oauth_idp.mint_jwt(aud=audience, sub=granted_sub)).request_raw("GET", "/api/system/kinds")
    assert granted.status_code == 200, f"a provisioned issuer JWT must authenticate: {granted.status_code}"

    # A policy under the BARE subject does NOT grant access — the JWT resolves to the
    # namespaced id, which has no policy, so it is authenticated-but-unauthorized (403).
    bare_sub = uniq("robot")
    await admin.post(
        "/api/auth/api-keys",
        json={"user_id": bare_sub, "description": "bare-sub policy (must not apply)", "scopes": ["e2e-all"]},
    )
    bare = await client(oauth_idp.mint_jwt(aud=audience, sub=bare_sub)).request_raw("GET", "/api/system/kinds")
    assert bare.status_code == 403, f"a bare-sub policy must not grant a namespaced JWT: {bare.status_code}"

    # An unknown subject (no policy anywhere) is denied.
    unknown = await client(oauth_idp.mint_jwt(aud=audience, sub=uniq("robot"))).request_raw("GET", "/api/system/kinds")
    assert unknown.status_code == 403, f"an unknown subject must be denied: {unknown.status_code}"

    # A JWT that fails verification never authenticates — a clean 401, not a 403.
    expired = await client(oauth_idp.mint_jwt(aud=audience, sub=granted_sub, expired=True)).request_raw(
        "GET", "/api/system/kinds"
    )
    assert expired.status_code == 401, f"an expired JWT must 401: {expired.status_code}"
    bad_sig = await client(oauth_idp.mint_jwt(aud=audience, sub=granted_sub, bad_signature=True)).request_raw(
        "GET", "/api/system/kinds"
    )
    assert bad_sig.status_code == 401, f"a bad-signature JWT must 401: {bad_sig.status_code}"
