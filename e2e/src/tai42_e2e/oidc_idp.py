"""A deterministic in-process OIDC issuer + OAuth2 authorization server, thread-
hosted in the pytest process. Signs RS256 tokens, serves discovery/JWKS/authorize/
token/userinfo, and mints good and deliberately-broken tokens for the negatives."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from joserfc import jwt as jose_jwt
from joserfc.jwk import RSAKey

from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.ports import allocate_port

# The fixed key id the issuer publishes in its JWKS and stamps in every token
# header; the verifier resolves the signing key by this kid.
_IDP_KID = "e2e-idp-key-1"
# The lifetime stamped onto a freshly minted id_token / machine JWT (``exp`` =
# ``iat`` + this). A negative clock offset (``set_clock_offset``) or ``expired=True``
# on ``mint_jwt`` moves ``exp`` into the past instead.
_ID_TOKEN_TTL_SECONDS = 3600


@dataclass
class _OAuthState:
    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    refresh_count: int = 0
    expires_in: int = 3600
    issued_access_tokens: list[str] = field(default_factory=list)
    # Per-authorization-code record (the OIDC ``nonce`` the /authorize request
    # carried), so /token can echo it into the id_token — the id_token replay
    # binding the OIDC login callback verifies. Popped single-use at exchange.
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    # When set, ``/authorize`` redirects back with an OAuth error instead of a
    # code — the scripted user-denies-consent path the browser-e2e negative drives.
    deny: bool = False


class OAuthIdp:
    """A deterministic in-memory OIDC issuer + OAuth2 authorization server.

    OAuth2 surface (the connector suite): ``/authorize`` redirects back with a
    code, ``/token`` grants and refreshes access tokens, ``/userinfo`` returns a
    fixed identity, and refresh grants are counted for the refresh-lock test.

    OIDC surface (the accounts/identity OIDC suites): a per-instance RSA keypair
    signs RS256 tokens, ``/.well-known/openid-configuration`` advertises discovery,
    ``/jwks`` publishes the public key under a fixed ``kid``, and every ``/token``
    response carries a signed ``id_token`` (issuer = ``base_url``, ``aud`` = the
    construction ``client_id``, ``sub``/``email`` construction-fixed, ``iat``/``exp``
    from a controllable clock offset, ``nonce`` echoed from the authorize request).
    :meth:`mint_jwt` produces good AND deliberately-broken machine JWTs directly,
    without an HTTP round trip, for the assert-the-negative specs."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int | None = None,
        *,
        client_id: str = "e2e-client",
        subject: str = "e2e-user",
        email: str = "e2e@example.com",
    ) -> None:
        self.host = host
        # A caller may pin the port (the browser-e2e runner does, so a Playwright
        # spec can flip the denial knob over HTTP at a known origin); otherwise
        # take an ephemeral one.
        self.port = port if port is not None else allocate_port()
        self.client_id = client_id
        self.subject = subject
        self.email = email
        # The default audience of tokens this issuer mints is the client it was
        # constructed for; a spec targeting the identity-oidc provider (whose
        # audience is configured independently) overrides it via ``mint_jwt(aud=…)``.
        self._audience = client_id
        # The signing keypair, per instance. ``_bad_key`` carries the SAME kid, so a token
        # it signs resolves the published key by kid and then fails the signature check (a
        # signature failure, not an unknown-kid failure).
        self._key = RSAKey.generate_key(2048, {"kid": _IDP_KID, "use": "sig", "alg": "RS256"})
        self._bad_key = RSAKey.generate_key(2048, {"kid": _IDP_KID, "use": "sig", "alg": "RS256"})
        # Seconds added to ``iat``/``exp`` on every minted token; a large negative
        # value makes tokens already-expired at the verifier without racing a real clock.
        self._clock_offset_seconds = 0
        # When true, the ``/token`` id_token is signed with the off-JWKS key (same
        # kid, wrong key) so the login callback resolves a key and then fails the
        # signature check. Default false leaves the normal good-signature flow intact.
        self._sign_id_token_badly = False
        self._state = _OAuthState()
        self._server = ThreadedServer(self._build_app(), host, self.port)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def refresh_count(self) -> int:
        return self._state.refresh_count

    @property
    def issued_access_tokens(self) -> list[str]:
        """Every access token this IdP has minted — the encryption-at-rest test
        asserts none of them appears verbatim in the stored connection blob."""
        return list(self._state.issued_access_tokens)

    def set_expires_in(self, seconds: int) -> None:
        """Set the lifetime of freshly issued access tokens (a value ``<= 0``
        makes the next-issued token already expired, forcing a refresh)."""
        self._state.expires_in = seconds

    def set_clock_offset(self, seconds: int) -> None:
        """Shift the ``iat``/``exp`` stamped on every minted id_token / machine JWT
        by ``seconds`` (a large negative value mints already-expired tokens through
        the /token flow, the login-flow analogue of ``mint_jwt(expired=True)``)."""
        self._clock_offset_seconds = seconds

    def set_deny(self, deny: bool) -> None:
        """Script the authorization decision: when ``deny`` is true the next
        ``/authorize`` redirects back with ``error=access_denied`` (no code)
        instead of granting one — the user-refused-consent negative."""
        self._state.deny = deny

    def set_sign_id_token_badly(self, bad: bool) -> None:
        """Script the ``/token`` id_token signature: when ``bad`` is true the id_token
        the token endpoint returns is signed with the off-JWKS key (same kid), so the
        login callback resolves the published key and then fails the signature check —
        the bad-signature id_token negative driven through the full authorize flow."""
        self._sign_id_token_badly = bad

    def jwks(self) -> dict[str, Any]:
        """The published JWK set (public key only), as ``/jwks`` serves it."""
        return {"keys": [self._key.as_dict(private=False)]}

    def mint_jwt(
        self,
        *,
        sub: str | None = None,
        aud: str | None = None,
        expired: bool = False,
        bad_signature: bool = False,
        nonce: str | None = None,
        extra_claims: dict[str, Any] | None = None,
    ) -> str:
        """Mint a signed RS256 JWT directly (no HTTP round trip).

        ``sub``/``aud`` default to the construction subject/client; ``expired`` puts
        ``exp`` in the past; ``bad_signature`` signs with an off-JWKS key of the same
        kid (so verification resolves a key and then fails the signature check).
        The assert-the-negative OIDC specs use this to produce good AND bad tokens."""
        return self._sign(
            sub=sub if sub is not None else self.subject,
            aud=aud if aud is not None else self._audience,
            expired=expired,
            bad_signature=bad_signature,
            nonce=nonce,
            extra_claims=extra_claims,
        )

    def _sign(
        self,
        *,
        sub: str,
        aud: str,
        expired: bool,
        bad_signature: bool,
        nonce: str | None,
        extra_claims: dict[str, Any] | None,
    ) -> str:
        now = int(time.time()) + self._clock_offset_seconds
        exp = now - _ID_TOKEN_TTL_SECONDS if expired else now + _ID_TOKEN_TTL_SECONDS
        claims: dict[str, Any] = {
            "iss": self.base_url,
            "aud": aud,
            "sub": sub,
            "email": self.email,
            "iat": now,
            "exp": exp,
        }
        if nonce is not None:
            claims["nonce"] = nonce
        if extra_claims:
            claims.update(extra_claims)
        key = self._bad_key if bad_signature else self._key
        return jose_jwt.encode({"alg": "RS256", "kid": _IDP_KID}, claims, key)

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/.well-known/openid-configuration")
        async def discovery() -> JSONResponse:
            base = self.base_url
            return JSONResponse(
                {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                    "userinfo_endpoint": f"{base}/userinfo",
                    "jwks_uri": f"{base}/jwks",
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                }
            )

        @app.get("/jwks")
        async def jwks() -> JSONResponse:
            return JSONResponse(self.jwks())

        @app.get("/authorize")
        async def authorize(redirect_uri: str, state: str = "", nonce: str = "") -> RedirectResponse:
            sep = "&" if "?" in redirect_uri else "?"
            if self._state.deny:
                # The user refused consent: return the OAuth error redirect the
                # callback page relays back to the app, never a code.
                return RedirectResponse(url=f"{redirect_uri}{sep}error=access_denied&state={state}", status_code=302)
            code = f"code-{uuid.uuid4().hex[:8]}"
            # Remember the nonce so /token can bind it into the id_token — the OIDC
            # replay binding the login callback verifies against its stored nonce.
            self._state.codes[code] = {"nonce": nonce}
            return RedirectResponse(url=f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)

        @app.post("/token")
        async def token(request: Request) -> JSONResponse:
            grant = await _form_or_json(request)
            grant_type = grant.get("grant_type")
            if grant_type == "refresh_token":
                self._state.refresh_count += 1
            access = f"at-{uuid.uuid4().hex}"
            refresh = f"rt-{uuid.uuid4().hex}"
            self._state.issued_access_tokens.append(access)
            # Echo the authorize nonce (single-use) into the signed id_token; a
            # refresh grant carries no code and so no nonce.
            code = grant.get("code")
            record = self._state.codes.pop(code, None) if isinstance(code, str) else None
            nonce = record.get("nonce") if record else None
            id_token = self._sign(
                sub=self.subject,
                aud=self._audience,
                expired=False,
                bad_signature=self._sign_id_token_badly,
                nonce=nonce or None,
                extra_claims=None,
            )
            return JSONResponse(
                {
                    "access_token": access,
                    "refresh_token": refresh,
                    "id_token": id_token,
                    "token_type": "Bearer",
                    "expires_in": self._state.expires_in,
                    "scope": "read",
                }
            )

        @app.get("/userinfo")
        async def userinfo() -> JSONResponse:
            return JSONResponse({"sub": self.subject, "email": self.email})

        # Out-of-process control: a Playwright spec in a separate Node process flips the
        # denial knob over HTTP (the in-process ``set_deny`` cannot cross the boundary).
        @app.post("/_deny")
        async def deny_authorize() -> JSONResponse:
            self.set_deny(True)
            return JSONResponse({"deny": True})

        @app.post("/_allow")
        async def allow_authorize() -> JSONResponse:
            self.set_deny(False)
            return JSONResponse({"deny": False})

        return app


async def _form_or_json(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    form = await request.form()
    return dict(form.items())
