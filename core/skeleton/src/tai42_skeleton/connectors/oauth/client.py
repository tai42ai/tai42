"""OAuth 2.0 + PKCE primitives for Connectors (stdlib + pooled HTTP, no 3rd-party OAuth lib).

Covers PKCE pair generation, authorize-URL construction with redirect-URI
allow-list enforcement, code/refresh token exchange, and revocation. Every
failure path logs once at WARNING/ERROR before raising a typed exception.

The endpoints and the client come resolved (``authorization.resolve_authorization`` /
``resolve_client``); nothing here reads a descriptor's endpoints or env names. A declared
provider's requests go through the app-pooled :class:`HttpxClient` (one TLS session-cache and
connection pool, torn down centrally at shutdown); a discovered provider's go through the kit URL
guard, since a remote server named every one of its URLs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import quote_plus, urlencode, urlparse

import httpx
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.net import GuardedRequestError, UrlGuardError, guarded_request

from tai42_skeleton.connectors.settings import connector_engine_config

if TYPE_CHECKING:
    from tai42_contract.connectors.models import UpstreamRevokeOutcome
    from tai42_contract.connectors.providers import ProviderDescriptor

    from tai42_skeleton.connectors.oauth.authorization import ResolvedAuthorization, ResolvedClient

logger = logging.getLogger(__name__)

OAUTH_HTTP_TIMEOUT_SECONDS = 20.0
# Cap on a token-endpoint answer read through the URL guard.
_TOKEN_MAX_BYTES = 64 * 1024

# Free-form ``error_description`` (RFC 6749) can echo the auth code or token;
# truncate before logging so a misbehaving provider cannot leak one.
_ERROR_DESCRIPTION_MAX_LEN = 64


def _http() -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """The app-pooled OAuth HTTP client.

    One TLS session-cache + connection pool shared across all OAuth calls,
    closed centrally at shutdown.
    """
    return client_ctx(HttpxClient, timeout=OAUTH_HTTP_TIMEOUT_SECONDS)


def _error_detail(resp: _TokenAnswer) -> str:
    """Log-safe summary of a non-2xx OAuth body: only ``error`` + a truncated ``error_description``.

    Everything else is dropped to avoid leaking tokens.
    """
    try:
        payload = resp.json()
    except Exception:
        return f"non_json body_len={len(resp.content)}"
    if not isinstance(payload, dict):
        return f"non_object body_len={len(resp.content)}"
    error = payload.get("error")
    desc = payload.get("error_description")
    if isinstance(desc, str) and len(desc) > _ERROR_DESCRIPTION_MAX_LEN:
        desc = desc[:_ERROR_DESCRIPTION_MAX_LEN] + "…"
    if isinstance(error, str) and isinstance(desc, str):
        return f"error={error!r} error_description={desc!r}"
    if isinstance(error, str):
        return f"error={error!r}"
    return "no_error_field"


# -- Errors -------------------------------------------------------------------


class OAuthError(RuntimeError):
    """Base class for connector OAuth failures."""


class RedirectUriNotAllowedError(OAuthError):
    """The configured/computed redirect URI is not on the allow-list."""


class CodeExchangeFailedError(OAuthError):
    """Provider rejected the authorization-code → token exchange.

    ``error_code`` is the provider's RFC 6749 ``error`` when its answer carried one.
    """

    def __init__(self, message: str, *, error_code: str | None = None):
        """Build the error with the provider's ``error_code``, when known."""
        super().__init__(message)
        self.error_code = error_code


class RefreshTokenMissingError(OAuthError):
    """Provider returned no refresh_token on first consent."""


RefreshFailureReason = Literal["invalid_grant", "invalid_client", "transient"]


class TokenRefreshFailedError(OAuthError):
    """Provider rejected the refresh_token grant.

    ``reason`` is ``"invalid_grant"`` when the refresh token is revoked/expired
    (→ Reconnect required), ``"invalid_client"`` when a dynamically registered
    client is gone (→ Reconnect required), or ``"transient"`` for everything to
    retry. The resolver uses ``reason`` to dispatch the right state transition;
    ``error_code`` is the provider's RFC 6749 ``error`` when its answer carried one.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: RefreshFailureReason,
        http_status: int | None = None,
        error_code: str | None = None,
    ):
        """Build the error with its ``reason``, the optional ``http_status`` and the provider's ``error_code``."""
        super().__init__(message)
        self.reason: RefreshFailureReason = reason
        self.http_status = http_status
        self.error_code = error_code


# -- PKCE --------------------------------------------------------------------


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` using RFC 7636 S256.

    ``token_urlsafe(64)`` yields an 86-char verifier, inside the 43-128 range
    RFC 7636 §4.1 requires, so no length clamping is needed.
    """
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


# -- Redirect-URI allow-list -------------------------------------------------


def _is_local_host(host: str) -> bool:
    # Literal-only set so an attacker-registered ``evil.local`` cannot match.
    return host in {"localhost", "127.0.0.1", "::1"}


def validate_redirect_uri(redirect_uri: str) -> str:
    """Validate a redirect URI against the configured allow-list.

    Requires an exact origin match and https:// for any non-localhost host.
    Returns the normalised URI; raises :class:`RedirectUriNotAllowedError` on any
    rule violation (logged redacted to scheme+host).
    """
    if not redirect_uri:
        raise RedirectUriNotAllowedError("redirect_uri must be non-empty")

    parsed = urlparse(redirect_uri)
    if not parsed.scheme or not parsed.netloc:
        logger.warning(
            "connectors: redirect_uri rejected (unparseable): %s://%s",
            parsed.scheme,
            parsed.netloc,
        )
        raise RedirectUriNotAllowedError("redirect_uri must include scheme and host")

    host = parsed.hostname or ""
    if parsed.scheme != "https" and not _is_local_host(host):
        logger.warning(
            "connectors: redirect_uri rejected (non-https for non-local host): %s://%s",
            parsed.scheme,
            host,
        )
        raise RedirectUriNotAllowedError("redirect_uri must use https:// for non-local hosts")

    allowlist = connector_engine_config().redirect_uri_allowlist_origins
    if not allowlist:
        logger.warning(
            "connectors: redirect_uri rejected because CONNECTORS_REDIRECT_URI_ALLOWLIST is unset",
        )
        raise RedirectUriNotAllowedError(
            "CONNECTORS_REDIRECT_URI_ALLOWLIST env var is empty — operator must "
            "configure the allow-list before any Connect flow can start"
        )

    # Exact origin (scheme://host[:port]) match; path is ignored because the
    # callback page is served on a fixed path the deployment narrows.
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in allowlist:
        logger.warning(
            "connectors: redirect_uri rejected (origin %s not in allow-list)",
            origin,
        )
        raise RedirectUriNotAllowedError(f"redirect_uri origin {origin!r} is not in the allow-list")

    # Returned unchanged: OAuth requires the redirect_uri to be byte-identical
    # across authorize and token exchange, so this must not normalize (e.g. strip
    # a trailing slash) — every caller uses the exact validated string.
    return redirect_uri


# -- Authorize URL -----------------------------------------------------------


def build_authorize_url(
    *,
    descriptor: ProviderDescriptor,
    resolved: ResolvedAuthorization,
    client: ResolvedClient,
    scopes: list[str],
    state: str,
    code_challenge: str,
    redirect_uri: str,
) -> str:
    """Construct the authorize URL with PKCE, enforcing the redirect allow-list.

    ``resource`` is added for a discovered provider; ``scope`` is omitted when the list is empty; the
    descriptor's ``extra_authorize_params`` are applied last.
    """
    validate_redirect_uri(redirect_uri)

    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    if resolved.resource is not None:
        params["resource"] = resolved.resource
    params.update(descriptor.extra_authorize_params)
    endpoint = resolved.authorization_endpoint
    # RFC 6749 §3.1: an authorization endpoint's own query component is retained.
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}{urlencode(params)}"


# -- Token responses ---------------------------------------------------------

# Expiry assumed for a token answer without ``expires_in`` that carries (or inherits) a refresh token.
DEFAULT_EXPIRES_IN_SECONDS = 3600


@dataclass(frozen=True)
class TokenResponse:
    """A parsed OAuth token response: access/refresh tokens, expiry, granted scopes, and the raw body.

    ``expires_at`` is ``None`` only for a discovered provider whose answer carried neither
    ``expires_in`` nor a refresh token: valid until the server says otherwise.
    """

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    granted_scopes: list[str]
    raw: dict


def expiry_without_expires_in(*, has_refresh_token: bool, guarded: bool) -> datetime | None:
    """The expiry of an answer that carried no ``expires_in``.

    A declared provider, or a discovered one whose grant has a refresh token, assumes
    :data:`DEFAULT_EXPIRES_IN_SECONDS`; a discovered grant with no refresh token has no expiry.
    """
    if not guarded or has_refresh_token:
        return datetime.now(UTC) + timedelta(seconds=DEFAULT_EXPIRES_IN_SECONDS)
    return None


def _token_error_suffix(payload: dict) -> str:
    """Build a ``: error=... error_description=...`` suffix from the provider's RFC 6749 error fields.

    Uses the ``error`` / ``error_description`` fields when present, else ``""``
    — so a vendor that signals failure in a 200 body carrying no ``access_token``
    is loud with its reason. The description is truncated to bound a misbehaving
    provider echoing the code/token.
    """
    error = payload.get("error")
    desc = payload.get("error_description")
    parts: list[str] = []
    if isinstance(error, str) and error:
        parts.append(f"error={error!r}")
    if isinstance(desc, str) and desc:
        if len(desc) > _ERROR_DESCRIPTION_MAX_LEN:
            desc = desc[:_ERROR_DESCRIPTION_MAX_LEN] + "…"
        parts.append(f"error_description={desc!r}")
    return f": {' '.join(parts)}" if parts else ""


def _parse_token_response(
    payload: dict, *, fallback_refresh_token: str | None = None, guarded: bool = False
) -> TokenResponse:
    # ``token_type`` is not checked: a shipped vendor's token endpoint answers a non-"Bearer" value.
    access = payload.get("access_token")
    if not access:
        raise CodeExchangeFailedError("provider response is missing access_token" + _token_error_suffix(payload))
    if not isinstance(access, str):
        raise CodeExchangeFailedError(f"provider returned a non-string access_token (got {type(access).__name__})")
    refresh = payload.get("refresh_token") or fallback_refresh_token
    raw_expires_in = payload.get("expires_in")
    if raw_expires_in is None:
        expires_at = expiry_without_expires_in(has_refresh_token=refresh is not None, guarded=guarded)
    else:
        try:
            expires_in = int(raw_expires_in)
        except (TypeError, ValueError) as exc:
            raise CodeExchangeFailedError(f"provider returned a non-numeric expires_in: {raw_expires_in!r}") from exc
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    # A missing/null scope is tolerated (empty grant list); a present-but-wrong
    # type (e.g. a list) is a malformed body, not a silent AttributeError.
    raw_scope = payload.get("scope")
    if raw_scope is None:
        granted: list[str] = []
    elif isinstance(raw_scope, str):
        granted = [s for s in raw_scope.split(" ") if s]
    else:
        raise CodeExchangeFailedError(f"provider returned a non-string scope (got {type(raw_scope).__name__})")
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        granted_scopes=granted,
        raw=payload,
    )


# -- Token-endpoint transport --------------------------------------------------


class _TokenAnswer(Protocol):
    """A complete token-endpoint answer, from the pooled client or a guarded request alike."""

    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...

    def json(self) -> Any: ...


class _GuardedAnswer:
    """A guarded request's answer in the shape of an ``httpx.Response``."""

    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content

    def json(self) -> Any:
        return json.loads(self.content)


class TokenEndpointRefusedError(Exception):
    """The URL guard refused a token, revocation or registration endpoint a remote server named."""


async def _post(
    resolved: ResolvedAuthorization,
    url: str,
    *,
    form: dict[str, str],
    headers: dict[str, str],
    timeout: float = OAUTH_HTTP_TIMEOUT_SECONDS,
) -> _TokenAnswer:
    """POST ``form`` to ``url``: through the URL guard for a discovered provider, else the pooled client.

    Raises :class:`TokenEndpointRefusedError` when the guard refuses the URL and ``httpx.HTTPError``
    (or a guarded transport error) when the request fails.
    """
    if resolved.guarded:
        try:
            answer = await guarded_request(
                "POST", url, form=form, headers=headers, timeout=timeout, max_bytes=_TOKEN_MAX_BYTES
            )
        except UrlGuardError as exc:
            raise TokenEndpointRefusedError(str(exc)) from exc
        return _GuardedAnswer(answer.status, answer.body)
    async with _http() as http:
        return await http.post(url, data=form, headers=headers)


def _client_auth(client: ResolvedClient) -> tuple[dict[str, str], dict[str, str]]:
    """The token-endpoint client authentication: form fields and headers, by ``client.auth_method``."""
    form = {"client_id": client.client_id}
    headers: dict[str, str] = {}
    if client.auth_method == "none":
        return form, headers
    if client.client_secret is None:
        raise RuntimeError(f"client {client.client_id!r} authenticates with {client.auth_method} but has no secret")
    if client.auth_method == "client_secret_post":
        form["client_secret"] = client.client_secret
        return form, headers
    # RFC 6749 §2.3.1: id and secret, each form-urlencoded, joined by ":" and base64-encoded.
    pair = f"{quote_plus(client.client_id)}:{quote_plus(client.client_secret)}"
    headers["Authorization"] = "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")
    return form, headers


def _payload_error(answer: _TokenAnswer) -> str | None:
    try:
        payload = answer.json()
    except ValueError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"]
    return None


# -- Code → token exchange ---------------------------------------------------


async def exchange_code(
    *,
    descriptor: ProviderDescriptor,
    resolved: ResolvedAuthorization,
    client: ResolvedClient,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    resource: str | None = None,
    require_refresh_token: bool = True,
) -> TokenResponse:
    """Exchange an authorization code for tokens.

    ``resource`` is sent when the login has one. ``require_refresh_token``: the first Connect of a
    declared provider must return a refresh token; if it doesn't we abort rather than persist a
    half-broken record.
    """
    auth_form, auth_headers = _client_auth(client)
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        **auth_form,
        "code_verifier": code_verifier,
    }
    if resource is not None:
        body["resource"] = resource
    try:
        # RFC 6749 §5.1: the token endpoint response is JSON. Some authorization
        # servers default to form-encoding without an explicit Accept header, so
        # request JSON to keep the body parseable across providers.
        answer = await _post(
            resolved, resolved.token_endpoint, form=body, headers={"Accept": "application/json", **auth_headers}
        )
    except TokenEndpointRefusedError as exc:
        logger.warning("connectors: token exchange refused by the URL guard (%s): %s", descriptor.id, exc)
        raise CodeExchangeFailedError(f"token endpoint refused: {exc}") from exc
    except (httpx.HTTPError, GuardedRequestError) as exc:
        logger.error(
            "connectors: token exchange transport error (%s)",
            descriptor.id,
            exc_info=True,
        )
        raise CodeExchangeFailedError(f"transport error: {exc}") from exc

    if answer.status_code != 200:
        logger.warning(
            "connectors: token exchange failed (%s) status=%s detail=%s",
            descriptor.id,
            answer.status_code,
            _error_detail(answer),
        )
        error_code = _payload_error(answer)
        raise CodeExchangeFailedError(
            f"provider returned status {answer.status_code}" + (f" error={error_code!r}" if error_code else ""),
            error_code=error_code,
        )

    try:
        raw = answer.json()
    except ValueError as exc:
        logger.warning(
            "connectors: token exchange returned non-JSON 200 body (%s)",
            descriptor.id,
            exc_info=True,
        )
        raise CodeExchangeFailedError("provider returned a non-JSON body") from exc
    if not isinstance(raw, dict):
        raise CodeExchangeFailedError(f"provider returned a non-object body (got {type(raw).__name__})")

    parsed = _parse_token_response(raw, guarded=resolved.guarded)
    if require_refresh_token and not parsed.refresh_token:
        logger.warning(
            "connectors: provider %s returned no refresh_token on first consent",
            descriptor.id,
        )
        raise RefreshTokenMissingError("provider returned no refresh_token; aborting Connect")
    return parsed


# -- Refresh -----------------------------------------------------------------

# One total deadline for a refresh request of either kind of provider: the pooled client's own
# timeout is per phase, which a server that drips its answer never trips.
REFRESH_DEADLINE_SECONDS = OAUTH_HTTP_TIMEOUT_SECONDS


async def _send_refresh(
    descriptor: ProviderDescriptor, resolved: ResolvedAuthorization, form: dict[str, str], headers: dict[str, str]
) -> _TokenAnswer:
    """Send the refresh request within :data:`REFRESH_DEADLINE_SECONDS`; every transport failure is transient."""
    try:
        async with asyncio.timeout(REFRESH_DEADLINE_SECONDS):
            # RFC 6749 §5.1: request a JSON token response (some servers default to
            # form-encoding without the header).
            return await _post(
                resolved,
                resolved.token_endpoint,
                form=form,
                headers={"Accept": "application/json", **headers},
                timeout=REFRESH_DEADLINE_SECONDS,
            )
    except TimeoutError as exc:
        logger.warning("connectors: refresh timed out after %ss (%s)", REFRESH_DEADLINE_SECONDS, descriptor.id)
        raise TokenRefreshFailedError("transport error: refresh deadline exceeded", reason="transient") from exc
    except TokenEndpointRefusedError as exc:
        logger.warning("connectors: refresh token endpoint refused by the URL guard (%s): %s", descriptor.id, exc)
        raise TokenRefreshFailedError(f"token endpoint refused: {exc}", reason="transient") from exc
    except (httpx.HTTPError, GuardedRequestError) as exc:
        logger.warning(
            "connectors: refresh transport error (%s)",
            descriptor.id,
            exc_info=True,
        )
        raise TokenRefreshFailedError(f"transport error: {exc}", reason="transient") from exc


def _refresh_rejection(
    descriptor: ProviderDescriptor, answer: _TokenAnswer, client: ResolvedClient
) -> TokenRefreshFailedError:
    """Classify a non-200 refresh answer.

    RFC 6749 §5.2: ``invalid_grant`` means the refresh token itself is dead (terminal → Reconnect);
    ``invalid_client`` is terminal only for a dynamically registered client (its registration is
    gone) — an env, manual or document client may be mid-rotation. Everything else (5xx, 401, 403,
    429, other 400s) is operational — back off and retry.
    """
    if 500 <= answer.status_code < 600:
        logger.warning(
            "connectors: refresh transient failure (%s) status=%s",
            descriptor.id,
            answer.status_code,
        )
        return TokenRefreshFailedError(
            f"provider returned status {answer.status_code}",
            reason="transient",
            http_status=answer.status_code,
        )
    err = _payload_error(answer) or ""
    reason: RefreshFailureReason
    if err == "invalid_grant":
        reason = "invalid_grant"
    elif err == "invalid_client" and client.source == "dynamic":
        reason = "invalid_client"
    else:
        reason = "transient"
    logger.warning(
        "connectors: refresh %s (%s) status=%s error=%r",
        reason,
        descriptor.id,
        answer.status_code,
        err,
    )
    return TokenRefreshFailedError(
        f"provider returned status {answer.status_code} error={err!r}",
        reason=reason,
        http_status=answer.status_code,
        error_code=err or None,
    )


def _refreshed_tokens(
    descriptor: ProviderDescriptor, answer: _TokenAnswer, *, refresh_token: str, guarded: bool
) -> TokenResponse:
    """Parse a 200 refresh answer; a provider may omit refresh_token on refresh — the old one is kept."""
    try:
        raw = answer.json()
    except ValueError as exc:
        logger.warning(
            "connectors: refresh returned non-JSON 200 body (%s)",
            descriptor.id,
            exc_info=True,
        )
        raise TokenRefreshFailedError(
            "provider returned a non-JSON body",
            reason="transient",
        ) from exc
    if not isinstance(raw, dict):
        raise TokenRefreshFailedError(
            f"provider returned a non-object body (got {type(raw).__name__})", reason="transient"
        )
    try:
        return _parse_token_response(raw, fallback_refresh_token=refresh_token, guarded=guarded)
    except CodeExchangeFailedError as exc:
        # ``_parse_token_response`` is shared with the exchange path and raises
        # CodeExchangeFailedError on a malformed body; re-map it to the refresh path's
        # single documented error contract so it never escapes unclassified.
        logger.warning(
            "connectors: refresh returned a malformed 200 body (%s)",
            descriptor.id,
            exc_info=True,
        )
        raise TokenRefreshFailedError(str(exc), reason="transient") from exc


async def refresh(
    *,
    descriptor: ProviderDescriptor,
    resolved: ResolvedAuthorization,
    client: ResolvedClient,
    refresh_token: str,
    resource: str | None = None,
) -> TokenResponse:
    """Exchange a refresh_token for a fresh access token, within one total deadline.

    Raises :class:`TokenRefreshFailedError` on every error, with ``reason`` set to
    ``"invalid_grant"`` on revocation/expiry, ``"invalid_client"`` when a dynamically registered
    client is refused, or ``"transient"`` on everything else (network, 5xx, the deadline, a refused
    URL) so the caller can dispatch to the right state transition.
    """
    auth_form, auth_headers = _client_auth(client)
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        **auth_form,
    }
    if resource is not None:
        body["resource"] = resource
    answer = await _send_refresh(descriptor, resolved, body, auth_headers)
    if answer.status_code != 200:
        raise _refresh_rejection(descriptor, answer, client)
    return _refreshed_tokens(descriptor, answer, refresh_token=refresh_token, guarded=resolved.guarded)


# -- Revoke ------------------------------------------------------------------


@dataclass(frozen=True)
class RevokeOutcome:
    """The result of an upstream token revocation attempt, with any HTTP status."""

    outcome: UpstreamRevokeOutcome
    http_status: int | None = None


async def revoke(
    *, descriptor: ProviderDescriptor, resolved: ResolvedAuthorization, client: ResolvedClient | None, token: str
) -> RevokeOutcome:
    """Best-effort upstream token revocation at ``resolved.revocation_endpoint``.

    A discovered provider's request carries the ``client`` authentication; a declared provider's
    sends only the token (``client`` may be ``None``). Never raises on an upstream failure — the
    caller proceeds with the local purge regardless of outcome.
    """
    if not resolved.revocation_endpoint:
        return RevokeOutcome(outcome="skipped")

    form = {"token": token}
    headers: dict[str, str] = {}
    if resolved.guarded:
        if client is None:
            raise RuntimeError(f"revoking a grant of discovered provider {descriptor.id!r} needs its client")
        auth_form, headers = _client_auth(client)
        form.update(auth_form)
    try:
        answer = await _post(resolved, resolved.revocation_endpoint, form=form, headers=headers)
    except TokenEndpointRefusedError as exc:
        logger.warning("connectors: revocation endpoint refused by the URL guard (%s): %s", descriptor.id, exc)
        return RevokeOutcome(outcome="failed")
    except (httpx.HTTPError, GuardedRequestError):
        logger.warning(
            "connectors: revoke transport error (%s)",
            descriptor.id,
            exc_info=True,
        )
        return RevokeOutcome(outcome="failed")

    if 200 <= answer.status_code < 300:
        return RevokeOutcome(outcome="success", http_status=answer.status_code)
    logger.warning(
        "connectors: revoke non-2xx (%s) status=%s detail=%s",
        descriptor.id,
        answer.status_code,
        _error_detail(answer),
    )
    return RevokeOutcome(outcome="failed", http_status=answer.status_code)
