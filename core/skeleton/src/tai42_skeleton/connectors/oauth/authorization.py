"""The one resolved-authorization seam every OAuth door of the connectors engine uses.

The four doors — the authorize URL, the code exchange, the refresh and the revoke — never read
``descriptor.oauth`` or the env client names themselves: they ask :func:`resolve_authorization` for
the endpoints (from the descriptor for a declared provider, from :func:`discover` for a discovered
one) and :func:`resolve_client` for the client. A caller already bound to an issuer (a login's, a
grant's) asks for that issuer; a descriptor's ``issuer`` pin wins over it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from tai42_contract.connectors.errors import ConnectorClientRequiredError, OperatorMisconfiguredError
from tai42_contract.connectors.models import ConnectionGrant, ConnectionRecord
from tai42_contract.connectors.providers import OAuthEndpoints, ProviderDescriptor
from tai42_contract.errors import ErrorKind

from tai42_skeleton.connectors.oauth.client import OAuthError
from tai42_skeleton.connectors.oauth.discovery import OAuthDiscoveryError, discover, same_issuer

ClientAuthMethod = Literal["client_secret_post", "client_secret_basic", "none"]
ClientSource = Literal["env", "manual", "document", "dynamic"]

OFFLINE_ACCESS = "offline_access"


@dataclass(frozen=True)
class ResolvedAuthorization:
    """The endpoints and facts one OAuth door needs, for a declared or a discovered provider.

    ``guarded`` marks a discovered provider: every request to these endpoints goes through the URL
    guard. A declared provider carries no issuer, no resource, no scopes from discovery and both
    capability flags ``False``.
    """

    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str | None
    registration_endpoint: str | None
    issuer: str | None
    resource: str | None
    challenge_scopes: tuple[str, ...] | None
    scopes_supported: tuple[str, ...]
    offline_access_supported: bool
    token_endpoint_auth_methods_supported: tuple[str, ...] | None
    client_id_document_supported: bool
    iss_parameter_supported: bool
    guarded: bool


@dataclass(frozen=True)
class ResolvedClient:
    """The OAuth client a door authenticates as.

    Its id, its secret (``None`` for a public client) and how it sends them.
    """

    client_id: str
    client_secret: str | None = field(repr=False)
    auth_method: ClientAuthMethod
    source: ClientSource


class OAuthIssuerMismatchError(OAuthError):
    """The login redirect's ``iss`` is not the issuer the login was started with, or is missing where required."""

    __tai_error_kind__ = ErrorKind.UNAUTHORIZED


class OAuthAuthorizationCancelledError(OAuthError):
    """The authorization server answered the login redirect with an ``error`` (the user cancelled or denied)."""

    __tai_error_kind__ = ErrorKind.CANCELLED


class GrantUnservableError(Exception):
    """A stored grant can no longer be refreshed or revoked under its provider's current descriptor.

    The grant belongs to another authorization server or another client than the descriptor names;
    nothing carrying a secret or a token was sent. A refresh takes the terminal transition.
    """


@dataclass(frozen=True)
class GrantAuthorization:
    """How a stored grant is refreshed or revoked: the resolved endpoints, its client and its ``resource``."""

    resolved: ResolvedAuthorization
    # The stored client the grant was issued to; ``None``: the env client.
    client_id: str | None
    resource: str | None


# -- endpoints -----------------------------------------------------------------------------------


def _declared_endpoints(descriptor: ProviderDescriptor) -> OAuthEndpoints:
    if descriptor.oauth is None:
        raise RuntimeError(f"provider {descriptor.id!r} has no oauth endpoints (not a declared oauth provider)")
    return descriptor.oauth


def _declared(descriptor: ProviderDescriptor) -> ResolvedAuthorization:
    endpoints = _declared_endpoints(descriptor)
    return ResolvedAuthorization(
        authorization_endpoint=endpoints.authorize,
        token_endpoint=endpoints.token,
        revocation_endpoint=endpoints.revoke,
        registration_endpoint=None,
        issuer=None,
        resource=None,
        challenge_scopes=None,
        scopes_supported=(),
        offline_access_supported=False,
        token_endpoint_auth_methods_supported=None,
        client_id_document_supported=False,
        iss_parameter_supported=False,
        guarded=False,
    )


async def resolve_authorization(
    descriptor: ProviderDescriptor, *, bound_issuer: str | None = None, fresh: bool = False
) -> ResolvedAuthorization:
    """Resolve the endpoints and facts of ``descriptor`` for one door.

    Declared: from ``descriptor.oauth``. Discovered: from :func:`discover` of the provider's MCP URL,
    bound to the descriptor's ``issuer`` pin, else to the caller's ``bound_issuer``; a bound issuer
    the server does not list raises ``OAuthDiscoveryError(step="issuer-mismatch")``. A no-auth
    descriptor raises ``RuntimeError`` (a caller bug).
    """
    if descriptor.kind != "oauth":
        raise RuntimeError(f"provider {descriptor.id!r} is a no-auth provider: it has no authorization")
    if not descriptor.is_discovered:
        return _declared(descriptor)
    found = await discover(descriptor.discovery_url, fresh=fresh, bound_issuer=descriptor.issuer or bound_issuer)
    return ResolvedAuthorization(
        authorization_endpoint=found.authorization_endpoint,
        token_endpoint=found.token_endpoint,
        revocation_endpoint=found.revocation_endpoint,
        registration_endpoint=found.registration_endpoint,
        issuer=found.issuer,
        resource=found.resource,
        challenge_scopes=found.challenge_scopes,
        scopes_supported=found.scopes_supported,
        offline_access_supported=found.offline_access_supported,
        token_endpoint_auth_methods_supported=found.token_endpoint_auth_methods_supported,
        client_id_document_supported=found.client_id_document_supported,
        iss_parameter_supported=found.iss_parameter_supported,
        guarded=True,
    )


# -- clients -------------------------------------------------------------------------------------


def _required_env(env_var: str, *, provider_id: str) -> str:
    """Read a required operator-supplied env var; an unset or empty value raises :class:`OperatorMisconfiguredError`."""
    value = os.environ.get(env_var, "")
    if not value:
        raise OperatorMisconfiguredError(env_var=env_var, provider_id=provider_id)
    return value


def _env_client(descriptor: ProviderDescriptor) -> ResolvedClient:
    if not descriptor.client_id_env or not descriptor.client_secret_env:
        raise RuntimeError(f"provider {descriptor.id!r} declares no env client")
    return ResolvedClient(
        client_id=_required_env(descriptor.client_id_env, provider_id=descriptor.id),
        client_secret=_required_env(descriptor.client_secret_env, provider_id=descriptor.id),
        auth_method="client_secret_post",
        source="env",
    )


async def resolve_client(
    descriptor: ProviderDescriptor,
    resolved: ResolvedAuthorization,
    *,
    redirect_uri: str | None = None,
    client_id: str | None = None,
) -> ResolvedClient:
    """Resolve the client a door authenticates as.

    A descriptor that names env client variables resolves the env client (``client_secret_post``;
    an unset variable raises :class:`OperatorMisconfiguredError`). ``client_id`` selects the stored
    client a grant was issued to (refresh, revoke) and ``redirect_uri`` the one for a new flow; a
    provider with no env client, or a grant issued to a stored client, needs a stored client, and
    with none available this raises :class:`ConnectorClientRequiredError`.
    """
    if descriptor.client_id_env or descriptor.client_secret_env:
        if client_id is not None:
            raise ConnectorClientRequiredError(
                descriptor.id, "the grant was issued to a stored client; the descriptor names an env client"
            )
        return _env_client(descriptor)
    raise ConnectorClientRequiredError(descriptor.id, f"no stored client for {resolved.issuer!r}")


# -- grants --------------------------------------------------------------------------------------


async def resolve_for_grant(descriptor: ProviderDescriptor, grant: ConnectionGrant | None) -> GrantAuthorization:
    """Resolve how a stored grant is refreshed or revoked under ``descriptor`` as it is now.

    Raises :class:`GrantUnservableError` when the grant belongs to another client or authorization
    server than the descriptor names (a stored-client grant under an env-client descriptor; a pin that
    names another issuer; an unpinned server that no longer lists the grant's issuer).
    ``OAuthDiscoveryError`` otherwise propagates (a pinned issuer the server does not list, or any
    other discovery failure): the grant may still recover. A grant made with the env client under a
    descriptor that is declared again is served the declared way, with no ``resource``.
    """
    if grant is not None and grant.client_id is not None and descriptor.client_id_env:
        raise GrantUnservableError(
            f"the grant was issued to the stored client {grant.client_id!r}; "
            f"provider {descriptor.id!r} names an env client"
        )
    if not descriptor.is_discovered:
        return GrantAuthorization(resolved=_declared(descriptor), client_id=None, resource=None)
    bound = grant.issuer if grant is not None else None
    try:
        resolved = await resolve_authorization(descriptor, bound_issuer=bound)
    except OAuthDiscoveryError as exc:
        if exc.step == "issuer-mismatch" and descriptor.issuer is None and grant is not None:
            raise GrantUnservableError(
                f"the server of provider {descriptor.id!r} no longer lists the grant's issuer {grant.issuer!r}"
            ) from exc
        raise
    if grant is None:
        return GrantAuthorization(resolved=resolved, client_id=None, resource=None)
    if not same_issuer(grant.issuer, resolved.issuer):
        raise GrantUnservableError(
            f"provider {descriptor.id!r} pins the issuer {resolved.issuer!r}; the grant was issued by {grant.issuer!r}"
        )
    return GrantAuthorization(resolved=resolved, client_id=grant.client_id, resource=grant.resource)


def grant_for(
    descriptor: ProviderDescriptor, resolved: ResolvedAuthorization, client: ResolvedClient
) -> ConnectionGrant | None:
    """The grant a new login records: ``None`` for a declared provider, else where and with which client it was made."""
    if not descriptor.is_discovered or resolved.issuer is None:
        return None
    return ConnectionGrant(
        issuer=resolved.issuer,
        resource=resolved.resource,
        client_id=None if client.source == "env" else client.client_id,
    )


# -- scopes --------------------------------------------------------------------------------------


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def requested_scopes(
    descriptor: ProviderDescriptor,
    sub_services: Iterable[str],
    resolved: ResolvedAuthorization,
    *,
    record: ConnectionRecord | None = None,
) -> list[str]:
    """The scopes a login asks for.

    The descriptor's scopes for the enabled sub-services when there are any (sent unchanged); else
    the scopes the server's challenge named; else the protected-resource document's
    ``scopes_supported``; else none. In the two discovered cases ``offline_access`` is added when the
    authorization server lists it. A reconnect or toggle (``record``) asks for the union of that
    list, the record's granted scopes and the scopes it is known to lack, order kept.
    """
    declared = sorted({scope for sub_id in sub_services for scope in descriptor.sub_services[sub_id].scopes})
    if declared:
        scopes = declared
    else:
        discovered = list(resolved.challenge_scopes or resolved.scopes_supported)
        if discovered and resolved.offline_access_supported and OFFLINE_ACCESS not in discovered:
            discovered.append(OFFLINE_ACCESS)
        scopes = discovered
    if record is None:
        return scopes
    return _unique([*scopes, *record.granted_scopes, *record.scopes_wanted])


# -- the issuer check on the login redirect -------------------------------------------------------


def check_redirect_issuer(*, recorded_issuer: str | None, iss_required: bool, iss: str | None) -> None:
    """Apply the authorization-response issuer check (RFC 9207) before the redirect is acted on.

    A login with no recorded issuer (a declared provider) is not checked. Otherwise a present ``iss``
    must equal the recorded issuer exactly, and an absent one is refused when the metadata said the
    server sends it. Raises :class:`OAuthIssuerMismatchError`.
    """
    if recorded_issuer is None:
        return
    if iss is None:
        if iss_required:
            raise OAuthIssuerMismatchError("the authorization response carries no iss although the server sends one")
        return
    if not same_issuer(iss, recorded_issuer):
        raise OAuthIssuerMismatchError("the authorization response's iss is not the issuer this login was started with")
