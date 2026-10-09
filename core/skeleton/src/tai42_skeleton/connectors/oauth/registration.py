"""Dynamic client registration (RFC 7591) at an authorization server's ``registration_endpoint``.

One client per redirect URI: the request lists exactly the flow's redirect URI. The request goes
through the kit URL guard (a remote server named the endpoint) under one total deadline; the
answer's ``client_secret`` is never logged, and a refusal is logged with the server's ``error``
code and a truncated description only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from tai42_contract.errors import ErrorKind
from tai42_kit.net import GuardedRequestError, UrlGuardError, guarded_request, is_loopback_host

from tai42_skeleton.connectors.oauth.client import (
    CLIENT_AUTH_METHODS,
    OAUTH_HTTP_TIMEOUT_SECONDS,
    ClientAuthMethod,
    OAuthError,
    provider_error_suffix,
)
from tai42_skeleton.connectors.settings import connector_engine_config

if TYPE_CHECKING:
    from tai42_contract.connectors.providers import ProviderDescriptor

    from tai42_skeleton.connectors.oauth.authorization import ResolvedAuthorization

logger = logging.getLogger(__name__)

# Cap on a registration answer read through the URL guard.
REGISTRATION_MAX_BYTES = 64 * 1024

# RFC 7591 §2: the method a registration that names none defaults to.
_DEFAULT_AUTH_METHOD: ClientAuthMethod = "client_secret_basic"


class ClientRegistrationError(OAuthError):
    """The authorization server did not register a usable client.

    ``status`` is the HTTP status of its answer (``None`` when no answer arrived) and
    ``error_code`` its RFC 7591 ``error`` when the answer carried one.
    """

    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR

    def __init__(self, message: str, *, status: int | None = None, error_code: str | None = None) -> None:
        """Build the error with the answer's ``status`` and ``error_code``, when known."""
        super().__init__(message)
        self.status = status
        self.error_code = error_code


@dataclass(frozen=True)
class RegisteredClient:
    """A client an authorization server registered for one redirect URI."""

    client_id: str
    client_secret: str | None = field(repr=False)
    auth_method: ClientAuthMethod
    # ``None``: the secret never expires.
    secret_expires_at: datetime | None
    redirect_uri: str


def application_type(redirect_uri: str) -> str:
    """``native`` when the redirect URI's host is a loopback host, else ``web``."""
    host = urlsplit(redirect_uri).hostname or ""
    return "native" if is_loopback_host(host) else "web"


def preferred_auth_method(advertised: Sequence[str] | None) -> ClientAuthMethod:
    """The first method the engine speaks that the server advertises; ``client_secret_basic`` when it lists none.

    A list that holds none of the engine's methods raises :class:`ClientRegistrationError`.
    """
    if advertised is None:
        return _DEFAULT_AUTH_METHOD
    for method in CLIENT_AUTH_METHODS:
        if method in advertised:
            return method
    raise ClientRegistrationError(
        f"the authorization server advertises no token endpoint authentication method the engine speaks "
        f"(it lists {list(advertised)!r})"
    )


def registration_request(resolved: ResolvedAuthorization, redirect_uri: str, scopes: Sequence[str]) -> dict[str, Any]:
    """The RFC 7591 client metadata sent for one flow's redirect URI."""
    body: dict[str, Any] = {
        "redirect_uris": [redirect_uri],
        "application_type": application_type(redirect_uri),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": preferred_auth_method(resolved.token_endpoint_auth_methods_supported),
        "client_name": connector_engine_config().client_name,
    }
    if scopes:
        body["scope"] = " ".join(scopes)
    return body


def _parse_answer(payload: Any, *, redirect_uri: str, status: int) -> RegisteredClient:
    if not isinstance(payload, dict):
        raise ClientRegistrationError("the registration answer is not a JSON object", status=status)
    answer = cast("dict[str, Any]", payload)
    client_id = answer.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise ClientRegistrationError("the registration answer carries no client_id", status=status)
    secret = answer.get("client_secret")
    if secret is not None and not isinstance(secret, str):
        raise ClientRegistrationError("the registration answer carries a non-string client_secret", status=status)
    method = answer.get("token_endpoint_auth_method", _DEFAULT_AUTH_METHOD)
    if method not in CLIENT_AUTH_METHODS:
        raise ClientRegistrationError(
            f"the authorization server registered the client for {method!r}, "
            f"a token endpoint authentication method the engine does not speak",
            status=status,
        )
    if method != "none" and not secret:
        raise ClientRegistrationError(
            f"the authorization server registered a {method} client without a client_secret", status=status
        )
    raw_expiry = answer.get("client_secret_expires_at", 0)
    if isinstance(raw_expiry, bool) or not isinstance(raw_expiry, int) or raw_expiry < 0:
        raise ClientRegistrationError(
            f"the registration answer carries an invalid client_secret_expires_at {raw_expiry!r}", status=status
        )
    return RegisteredClient(
        client_id=client_id,
        client_secret=secret or None,
        auth_method=cast("ClientAuthMethod", method),
        secret_expires_at=None if raw_expiry == 0 else datetime.fromtimestamp(raw_expiry, tz=UTC),
        redirect_uri=redirect_uri,
    )


async def register_client(
    descriptor: ProviderDescriptor,
    resolved: ResolvedAuthorization,
    redirect_uri: str,
    *,
    scopes: Sequence[str],
) -> RegisteredClient:
    """Register a client for ``redirect_uri`` at ``resolved.registration_endpoint``.

    ``scopes`` is what the client will ask for (sent as ``scope`` when non-empty). A 200 or 201
    JSON object with a non-empty ``client_id`` is accepted; every other answer, a refused URL and a
    transport failure raise :class:`ClientRegistrationError`.
    """
    endpoint = resolved.registration_endpoint
    if endpoint is None:
        raise ClientRegistrationError(f"provider {descriptor.id!r}: the authorization server offers no registration")
    body = registration_request(resolved, redirect_uri, scopes)
    try:
        answer = await guarded_request(
            "POST",
            endpoint,
            headers={"Accept": "application/json"},
            json_body=body,
            timeout=OAUTH_HTTP_TIMEOUT_SECONDS,
            max_bytes=REGISTRATION_MAX_BYTES,
        )
    except UrlGuardError as exc:
        logger.warning("connectors: registration endpoint refused by the URL guard (%s): %s", descriptor.id, exc)
        raise ClientRegistrationError(f"registration endpoint refused: {exc}") from exc
    except GuardedRequestError as exc:
        logger.warning("connectors: client registration transport error (%s): %s", descriptor.id, exc)
        raise ClientRegistrationError(f"registration transport error: {exc}") from exc

    try:
        payload = json.loads(answer.body)
    except ValueError:
        payload = None
    if answer.status not in (200, 201):
        error_code = payload.get("error") if isinstance(payload, dict) else None
        error_code = error_code if isinstance(error_code, str) else None
        detail = provider_error_suffix(payload) if isinstance(payload, dict) else ""
        logger.warning("connectors: client registration refused (%s) status=%s%s", descriptor.id, answer.status, detail)
        raise ClientRegistrationError(
            f"the authorization server refused the registration with status {answer.status}{detail}",
            status=answer.status,
            error_code=error_code,
        )
    registered = _parse_answer(payload, redirect_uri=redirect_uri, status=answer.status)
    logger.info(
        "connectors: registered a client for provider %s at %s (client_id=%s, method=%s)",
        descriptor.id,
        resolved.issuer,
        registered.client_id,
        registered.auth_method,
    )
    return registered
