"""Public login-flow routes — the ``/api/login/*`` namespace.

Three routes drive the OIDC/OAuth2 login: ``authorize`` (302 to the issuer with
state + PKCE + nonce), ``callback`` (code exchange, id_token verification, session
mint, 302 back with a one-time SSO code), and ``sso/exchange`` (the SPA trades
that code for the session — never a token in the URL).

Handlers take no settings argument: they reach the injected ``settings.redis``
through ``provider.provider_settings()``. Success bodies are ``{"data": {"token":
raw, "user_id": ...}}``. Every failure returns a generic envelope with the detailed
cause server-logged. Also registers the AC-required boot guard.
"""

from __future__ import annotations

import hmac
import logging
import time
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from tai42_contract.app import tai42_app
from tai42_kit.net.jwt import JwtError, verify_jwt

from tai42_accounts_oidc import oauth, provider
from tai42_accounts_oidc.presets import (
    GITHUB_AUTHORIZE_URL,
    GITHUB_TOKEN_URL,
    GITHUB_USERINFO_URL,
    ResolvedProvider,
)
from tai42_accounts_oidc.settings import accounts_oidc_settings

logger = logging.getLogger(__name__)

# The router's resolved absolute mount base, captured at import (the mount binding is
# live only then), so every self-referential path here follows an operator remap of
# the login route base instead of a hardcoded default.
_MOUNT_BASE = tai42_app.http.mount_base()

# The flow-binding cookie: an HttpOnly secret set at authorize, required to match
# the redeemed state's binding at callback. SameSite=Lax reaches the callback (a
# top-level GET from the issuer); the path scopes it to the login flow.
_FLOW_COOKIE = "tai42_oidc_flow"


def _flow_cookie_path() -> str:
    """The path the flow cookie is scoped to — the OIDC login subtree of the mount."""
    return f"{_MOUNT_BASE}/oidc"


def _callback_path(name: str) -> str:
    """The absolute callback path for provider ``name`` under the resolved mount."""
    return f"{_MOUNT_BASE}/oidc/{name}/callback"


def authorize_path(name: str) -> str:
    """The absolute authorize path for provider ``name`` under the resolved mount —
    the login button's ``href``, resolved through the router's mount so a remap
    moves it too."""
    return f"{_MOUNT_BASE}/oidc/{name}/authorize"


class SessionResponse(BaseModel):
    """The one-time login result: a raw session token and the user's id."""

    token: str
    user_id: str


class SsoExchangeBody(BaseModel):
    """The SPA's one-time SSO hand-back exchange for ``POST /api/login/sso/exchange``."""

    code: str


class _CallbackError(Exception):
    """A callback failure carrying a client status/message and a server-log detail."""

    def __init__(self, status_code: int, client_message: str, log_detail: str) -> None:
        super().__init__(log_detail)
        self.status_code = status_code
        self.client_message = client_message
        self.log_detail = log_detail


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _session_response(raw_token: str, user_id: str) -> JSONResponse:
    return JSONResponse({"data": {"token": raw_token, "user_id": user_id}})


def _state_key() -> bytes:
    # Validated non-None at provider construction; fail loud if that ever breaks.
    state_key = accounts_oidc_settings().state_key
    if state_key is None:  # pragma: no cover - provider __init__ guarantees this
        raise RuntimeError("TAI_ACCOUNTS_OIDC_STATE_KEY is unset at request time")
    return state_key.get_secret_value().encode()


def _public_base_url() -> str:
    base = accounts_oidc_settings().public_base_url
    if base is None:  # pragma: no cover - provider __init__ guarantees this
        raise RuntimeError("TAI_ACCOUNTS_OIDC_PUBLIC_BASE_URL is unset at request time")
    return base


def _redirect_uri(name: str) -> str:
    # From operator config, never the inbound Host/origin, so a Host-header-injecting
    # client cannot steer where the auth code lands.
    return _public_base_url() + _callback_path(name)


def _flow_cookie_secure() -> bool:
    # Secure flag tracks the served scheme: https gets Secure; loopback-http (the
    # only non-https form the settings accept) does not, so the cookie round-trips.
    return urlsplit(_public_base_url()).scheme == "https"


@tai42_app.http.custom_route(
    "/oidc/{provider}/authorize",
    methods=["GET"],
    summary="Begin an OIDC/OAuth2 login",
    tags=["login"],
    response_model=None,
)
async def oidc_authorize(request: Request) -> Response:
    """Mint state + PKCE + nonce and 302 to the configured provider's issuer.

    An unknown provider name is either misconfiguration or a probe — both are
    logged server-side before the generic 404. The ``redirect_uri`` is built from
    ``public_base_url`` config, never the request origin.
    """
    name = request.path_params["provider"]
    runtime = provider.get_runtime(name)
    if runtime is None:
        logger.warning("accounts-oidc: authorize for unknown provider %r", name)
        return _error("Unknown login provider", 404)

    config = runtime.config
    state_id = oauth.new_state_id()
    nonce = oauth.new_nonce()
    binding = oauth.new_browser_binding()
    verifier, challenge = oauth.pkce_pair()
    await provider.store_state(
        state_id,
        {
            "provider": name,
            "verifier": verifier,
            "nonce": nonce,
            "binding": binding,
            "created_at": int(time.time()),
        },
    )

    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": _redirect_uri(name),
        "scope": " ".join(config.scopes),
        "state": oauth.sign_state(state_id, _state_key()),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    # The id_token replay binding; GitHub (plain OAuth2, no id_token) carries no nonce.
    if config.kind == "oidc":
        params["nonce"] = nonce

    # Merge operator-configured static params (e.g. an org pin) from boot config.
    # The settings validator rejects any key in the flow-owned reserved set, so a
    # merge never overrides a param built above — enforce that invariant loudly.
    for key, value in config.extra_authorize_params.items():
        if key in params:
            raise RuntimeError(f"extra_authorize_params key {key!r} collides with a flow-owned authorize param")
        params[key] = value

    if config.kind == "github":
        authorize_endpoint = GITHUB_AUTHORIZE_URL
    else:
        authorize_endpoint = (await runtime.discovery()).authorization_endpoint

    response = RedirectResponse(f"{authorize_endpoint}?{urlencode(params)}", status_code=302)
    response.set_cookie(
        _FLOW_COOKIE,
        binding,
        max_age=provider.STATE_TTL_SECONDS,
        path=_flow_cookie_path(),
        httponly=True,
        secure=_flow_cookie_secure(),
        samesite="lax",
    )
    return response


@tai42_app.http.custom_route(
    "/oidc/{provider}/callback",
    methods=["GET"],
    summary="Complete an OIDC/OAuth2 login",
    tags=["login"],
    response_model=None,
)
async def oidc_callback(request: Request) -> Response:
    """Verify state, exchange the code, verify the id_token, mint a session, and
    302 back to ``/login?sso={code}``.

    No role or policy write happens here — an unknown subject stays denied until an
    operator provisions its policy (deny-by-default).
    """
    name = request.path_params["provider"]
    runtime = provider.get_runtime(name)
    if runtime is None:
        logger.warning("accounts-oidc: callback for unknown provider %r", name)
        return _error("Unknown login provider", 404)

    query = request.query_params
    if "error" in query:
        # The user cancelled at consent, or the issuer refused.
        logger.warning(
            "accounts-oidc: issuer error at %s callback: error=%r description=%r",
            name,
            query.get("error"),
            query.get("error_description"),
        )
        return _error("Login was cancelled or refused", 400)

    code = query.get("code")
    state = query.get("state")
    if not code or not state:
        logger.warning("accounts-oidc: callback for %s missing code/state", name)
        return _error("Invalid login callback", 400)

    state_id = oauth.verify_state(state, _state_key())
    if state_id is None:
        logger.warning("accounts-oidc: callback for %s has a tampered/invalid state", name)
        return _error("Invalid login callback", 400)

    record = await provider.take_state(state_id)
    if record is None:
        logger.warning("accounts-oidc: callback for %s has an absent/expired/replayed state", name)
        return _error("Invalid login callback", 400)

    # A state minted for provider A must never be redeemed at B's callback — verify
    # before any token exchange.
    if record.get("provider") != name:
        logger.warning(
            "accounts-oidc: state minted for %r redeemed at %r callback (cross-provider confusion)",
            record.get("provider"),
            name,
        )
        return _clear_flow_cookie(_error("Invalid login callback", 400))

    # Bind redemption to the browser that began the flow: the state's binding must
    # match the flow cookie. A missing/mismatched cookie is login-CSRF — reject
    # before any token exchange.
    if not _binding_matches(request, record):
        logger.warning("accounts-oidc: callback for %s failed browser-binding (login-CSRF / missing flow cookie)", name)
        return _clear_flow_cookie(_error("Invalid login callback", 400))

    try:
        user_id = await _resolve_identity(runtime.config, runtime, code, _redirect_uri(name), record)
    except _CallbackError as exc:
        logger.warning("accounts-oidc: callback for %s failed: %s", name, exc.log_detail)
        return _clear_flow_cookie(_error(exc.client_message, exc.status_code))

    token = await provider.mint_session(user_id)
    sso_code = oauth.new_sso_code()
    await provider.store_sso_code(sso_code, token, user_id)
    redirect = RedirectResponse(f"{_public_base_url()}/login?sso={sso_code}", status_code=302)
    return _clear_flow_cookie(redirect)


def _binding_matches(request: Request, record: dict) -> bool:
    """Whether the flow cookie constant-time-matches the state record's binding."""
    cookie = request.cookies.get(_FLOW_COOKIE)
    expected = record.get("binding")
    if not cookie or not isinstance(expected, str):
        return False
    return hmac.compare_digest(cookie, expected)


def _clear_flow_cookie(response: Response) -> Response:
    """Expire the single-use flow cookie once its state has been consumed."""
    response.delete_cookie(_FLOW_COOKIE, path=_flow_cookie_path())
    return response


async def _resolve_identity(
    config: ResolvedProvider,
    runtime: provider.ProviderRuntime,
    code: str,
    redirect_uri: str,
    record: dict,
) -> str:
    """Exchange ``code`` for the caller's namespaced ``oidc:{provider}:{sub}`` id."""
    if config.kind == "github":
        return await _github_identity(config, code, redirect_uri, record["verifier"])
    return await _oidc_identity(config, runtime, code, redirect_uri, record)


async def _oidc_identity(
    config: ResolvedProvider,
    runtime: provider.ProviderRuntime,
    code: str,
    redirect_uri: str,
    record: dict,
) -> str:
    discovery = await runtime.discovery()
    token_data = await _post_token(
        discovery.token_endpoint,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": config.client_id,
            "client_secret": config.client_secret.get_secret_value(),
            "code_verifier": record["verifier"],
        },
    )
    id_token = token_data.get("id_token")
    if not isinstance(id_token, str):
        raise _CallbackError(502, "Login failed", f"token response for {config.name} carried no id_token")
    try:
        claims = await verify_jwt(
            id_token,
            jwks=await runtime.jwks(),
            issuer=config.issuer or "",
            audience=config.client_id,
            expected_nonce=record["nonce"],
        )
    except JwtError as exc:
        raise _CallbackError(401, "Login failed", f"id_token verification failed for {config.name}: {exc}") from exc
    return _namespaced_id(config, claims.get(config.claim))


async def _github_identity(config: ResolvedProvider, code: str, redirect_uri: str, verifier: str) -> str:
    token_data = await _post_token(
        GITHUB_TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": config.client_id,
            "client_secret": config.client_secret.get_secret_value(),
            "code_verifier": verifier,
        },
    )
    access_token = token_data.get("access_token")
    if not isinstance(access_token, str):
        raise _CallbackError(502, "Login failed", f"github token response for {config.name} carried no access_token")
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(10.0)) as client:
            response = await client.get(
                GITHUB_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            )
    except httpx.HTTPError as exc:
        raise _CallbackError(502, "Login failed", f"github userinfo transport error: {exc}") from exc
    if response.status_code != 200:
        raise _CallbackError(502, "Login failed", f"github userinfo returned status {response.status_code}")
    userinfo = response.json()
    if not isinstance(userinfo, dict):
        raise _CallbackError(502, "Login failed", "github userinfo returned a non-object JSON")
    return _namespaced_id(config, userinfo.get(config.claim))


def _namespaced_id(config: ResolvedProvider, value: object) -> str:
    if value is None:
        raise _CallbackError(502, "Login failed", f"identity for {config.name} missing claim {config.claim!r}")
    return f"oidc:{config.name}:{value}"


async def _post_token(url: str, data: dict[str, str]) -> dict:
    """POST the token-exchange form and return the parsed JSON object, or raise a
    ``_CallbackError`` naming the transport/status/parse failure."""
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(10.0)) as client:
            response = await client.post(url, data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise _CallbackError(502, "Login failed", f"token endpoint transport error: {exc}") from exc
    if response.status_code != 200:
        raise _CallbackError(502, "Login failed", f"token endpoint returned status {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise _CallbackError(502, "Login failed", f"token endpoint returned non-JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise _CallbackError(502, "Login failed", "token endpoint returned a non-object JSON")
    return payload


@tai42_app.http.custom_route(
    "/sso/exchange",
    methods=["POST"],
    summary="Exchange a one-time SSO code for a session",
    tags=["login"],
    request_model=SsoExchangeBody,
    response_model=SessionResponse,
)
async def sso_exchange(request: Request) -> Response:
    """Trade the one-time SSO hand-back code for the frozen login response.

    Single-use by construction (``GETDEL``). A miss — expired, replayed, or
    unknown code — is a security-relevant signal, so the detailed cause is logged
    server-side before the generic 400.
    """
    try:
        body = SsoExchangeBody.model_validate(await request.json())
    except ValueError:
        return _error("invalid request body", 400)

    record = await provider.take_sso_code(body.code)
    if record is None:
        logger.warning("accounts-oidc: SSO exchange miss (expired/replayed/unknown code)")
        return _error("Invalid or expired login code", 400)
    return _session_response(record["token"], record["user_id"])


@tai42_app.lifecycle.on_startup
def _assert_accounts_provider_instantiated() -> None:
    """Fail boot loudly if the login routes are mounted but the provider was never
    instantiated — access control is disabled and the settings holder is empty."""
    if not provider.provider_settings_populated():
        raise RuntimeError(
            "tai42-accounts-oidc routes are mounted but its provider was never instantiated — "
            "the accounts kind requires ACCESS_CONTROL_ENABLE=true and 'accounts-oidc' present "
            "in ACCESS_CONTROL_AUTH_PROVIDERS"
        )
