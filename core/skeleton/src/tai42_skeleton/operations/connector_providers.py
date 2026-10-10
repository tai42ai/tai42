"""The provider door: look an MCP server up by its address, add it as a provider, remove it, set its client.

Four administrator-only operations over :mod:`tai42_skeleton.connectors.service.provider_service`. Each
refuses with the named 501 ``connectors-not-configured`` before anything else while the connector store is
not configured. An address must be ``https`` (``http`` only on a loopback host), with no credentials and no
fragment; it is refused 400 before any request is made. A failed discovery is a 400 naming the failed step
in plain words. The client secret of :func:`set_connector_client` is never logged, never answered and never
written to the manifest: it goes only to the sealed client store.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from tai42_contract.connectors.models import (
    AddServerRequest,
    DiscoveredServerView,
    DiscoverServerRequest,
    ProviderMutationResponse,
    RemoveProviderResponse,
    SetConnectorClientRequest,
)
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_kit.db import component_store_configured

from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.oauth import client_document, discovery, redirect
from tai42_skeleton.connectors.oauth.client import ClientAuthMethod
from tai42_skeleton.connectors.oauth.discovery import DiscoveredAuthorization, DiscoveryStep, OAuthDiscoveryError
from tai42_skeleton.connectors.service import connection_service, provider_service
from tai42_skeleton.connectors.service.provider_service import ProviderIdTakenError
from tai42_skeleton.connectors.store import client_store
from tai42_skeleton.connectors.store.client_store import ConnectorStoreNotConfiguredError
from tai42_skeleton.db import SKELETON_COMPONENT, not_configured_message
from tai42_skeleton.operations import BadRequestError, ConflictError, NotFoundError, NotSupportedError, operation
from tai42_skeleton.operations.adapter import validation_error_fields
from tai42_skeleton.operations.connectors import catalog_entries

logger = logging.getLogger(__name__)

_NOT_CONFIGURED_CODE = "connectors-not-configured"

# The code a set-client refusal carries when no sign-in service was chosen for the credentials; ``extra``
# names the ones the server lists.
_ISSUER_REQUIRED_CODE = "connector-client-issuer-required"

_DISCOVERY_FAILED_CODE = "connector-discovery-failed"

# What each failed discovery step means, in plain words.
_STEP_MESSAGES: dict[DiscoveryStep, str] = {
    "challenge": "the server could not be reached",
    "resource-metadata": (
        "the server publishes no OAuth metadata; a server without sign-in can be added as a plain MCP server instead"
    ),
    "resource-mismatch": "the server's metadata names a different resource",
    "authorization-server-metadata": "the server's sign-in service publishes no usable OAuth metadata",
    "issuer-mismatch": "the sign-in service named by the server does not match its metadata",
    "pkce-unsupported": "the server's sign-in service does not support PKCE (S256)",
    "url-refused": (
        "an address was refused by the outbound URL guard; set TAI_URL_GUARD_ALLOW_CIDRS to admit a private range"
    ),
}

_SECRET_METHODS: tuple[ClientAuthMethod, ...] = ("client_secret_post", "client_secret_basic")


def _require_store() -> None:
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message("connector store"), extra={"code": _NOT_CONFIGURED_CODE})


def _discovery_refusal(exc: OAuthDiscoveryError) -> BadRequestError:
    return BadRequestError(
        f"{_STEP_MESSAGES[exc.step]} ({exc})", extra={"code": _DISCOVERY_FAILED_CODE, "step": exc.step}
    )


def _validated[M](model: type[M], values: dict[str, Any]) -> M:
    try:
        return model.model_validate(values)  # type: ignore[attr-defined]
    except ValidationError as exc:
        # Field paths and error types only: a rejected value may be a secret.
        raise BadRequestError("invalid request", extra={"fields": validation_error_fields(exc)}) from exc


def _checked_url(url: str) -> str:
    try:
        return provider_service.check_server_url(url)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


async def _discover(url: str, *, bound_issuer: str | None = None) -> DiscoveredAuthorization:
    try:
        return await discovery.discover(url, fresh=True, bound_issuer=bound_issuer)
    except OAuthDiscoveryError as exc:
        raise _discovery_refusal(exc) from exc


def _provider(provider_id: str) -> ProviderDescriptor:
    try:
        return connection_service.get_provider(provider_id)
    except KeyError as exc:
        raise NotFoundError(f"provider {provider_id!r} not found") from exc


def _listed(found: DiscoveredAuthorization, issuer: str) -> str | None:
    """The server's own spelling of ``issuer`` when it lists it, else ``None``."""
    return next((server for server in found.authorization_servers if discovery.same_issuer(server, issuer)), None)


def _redirect_uri_for(origin: str | None) -> str | None:
    """The deployment's callback on ``origin`` when a sign-in can start from it, else ``None``."""
    if origin is None:
        return None
    try:
        redirect.validate_origin_allowed(origin)
        return oauth_client.validate_redirect_uri(redirect.callback_uri(origin))
    except oauth_client.RedirectUriNotAllowedError:
        return None


def _document_client(found: DiscoveredAuthorization, origin: str | None) -> bool:
    return found.client_id_document_supported and origin is not None and client_document.eligible(origin)


async def _entry(descriptor: ProviderDescriptor) -> dict[str, Any]:
    (entry,) = await catalog_entries([descriptor])
    return entry.model_dump(mode="json")


# -- look up -------------------------------------------------------------------------------------


@operation(
    summary="Look up an MCP server's sign-in",
    tags=["connectors"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=DiscoverServerRequest,
    response_model=DiscoveredServerView,
)
async def discover_connector_server(
    url: str | None = None, provider_id: str | None = None, origin: str | None = None
) -> dict[str, Any]:
    """Look up the sign-in of the MCP server at ``url``, or of the server of the discovered provider ``provider_id``.

    Persists nothing. ``origin`` is the deployment origin a sign-in would start from: it decides the
    callback address an app registered by hand must allow and whether this deployment's client-identity
    document can serve the login. An unknown provider is a 404; a declared one a 400.
    """
    _require_store()
    if url is not None and provider_id is None:
        view = await _look_up_address(url)
    elif provider_id is not None and url is None:
        view = await _look_up_provider(provider_id)
    else:
        raise BadRequestError("give exactly one of url or provider_id")
    registration_supported = view.found.registration_endpoint is not None
    return DiscoveredServerView(
        url=view.url,
        resource=view.found.resource,
        resource_name=view.found.resource_name,
        issuer=view.found.issuer,
        authorization_servers=list(view.found.authorization_servers),
        client_issuer=view.client_issuer,
        registration_supported=registration_supported,
        automatic_client=registration_supported or _document_client(view.found, origin) or view.has_client,
        scopes_supported=list(view.found.scopes_supported),
        suggested_id=view.suggested_id,
        suggested_display_name=view.suggested_name,
        redirect_uri=_redirect_uri_for(origin),
    ).model_dump(mode="json")


@dataclass(frozen=True)
class _LookUp:
    url: str
    found: DiscoveredAuthorization
    suggested_id: str
    suggested_name: str
    client_issuer: str | None = None
    # The provider already has a client a login from any origin can use (an env client, or a manual
    # client for the sign-in service a login would use).
    has_client: bool = False


async def _look_up_address(url: str) -> _LookUp:
    url = _checked_url(url)
    found = await _discover(url)
    return _LookUp(
        url=url,
        found=found,
        suggested_id=provider_service.suggest_provider_id(url, await provider_service.taken_provider_ids()),
        suggested_name=provider_service.suggested_display_name(url, found),
    )


async def _look_up_provider(provider_id: str) -> _LookUp:
    descriptor = _provider(provider_id)
    if not descriptor.is_discovered:
        raise BadRequestError(f"provider {provider_id!r} is not a discovered provider: it has no server to look up")
    url = descriptor.discovery_url
    names = {"suggested_id": descriptor.id, "suggested_name": descriptor.display_name}
    if descriptor.client_id_env:
        found = await _discover(url, bound_issuer=descriptor.issuer)
        return _LookUp(url=url, found=found, has_client=True, **names)
    # A stored-client provider's logins go to its manual client's sign-in service while the server lists it.
    found = await _discover(url)
    client_issuer = await client_store.active_client_store().manual_issuer(provider_id)
    listed = None if client_issuer is None else _listed(found, client_issuer)
    if listed is not None and not discovery.same_issuer(found.issuer, listed):
        found = await _discover(url, bound_issuer=listed)
    return _LookUp(url=url, found=found, client_issuer=client_issuer, has_client=listed is not None, **names)


# -- add -----------------------------------------------------------------------------------------


@operation(
    summary="Add an MCP server as a connector provider",
    tags=["connectors"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, ConflictError, NotSupportedError],
    request_model=AddServerRequest,
    response_model=ProviderMutationResponse,
)
async def add_connector_provider(
    url: str,
    provider_id: str | None = None,
    display_name: str | None = None,
    description: str = "",
    icon_url: str | None = None,
    category: str | None = None,
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    """Add the MCP server at ``url`` as a discovered provider, written to the manifest and broadcast to the fleet.

    Its id is ``provider_id`` or a suggestion from the host. An id that is not free — a provider in the
    manifest, a stored connection record (live or lapsed), or a stored client row under it — is a 409
    naming which; nothing is deleted.
    """
    _require_store()
    request = _validated(
        AddServerRequest,
        {
            "url": url,
            "id": provider_id,
            "display_name": display_name,
            "description": description,
            "icon_url": icon_url,
            "category": category,
            "scopes": scopes or [],
        },
    )
    _checked_url(request.url)
    found = await _discover(request.url)
    if request.id is not None:
        reason = await provider_service.id_taken_reason(request.id)
        if reason is not None:
            raise ConflictError(f"provider id {request.id!r} is not free: {reason}")
        new_id = request.id
    else:
        new_id = provider_service.suggest_provider_id(request.url, await provider_service.taken_provider_ids())
    try:
        descriptor = provider_service.build_descriptor(request, found, new_id)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    try:
        applied = await connection_service.ConfigService.from_app().apply_change(
            lambda document: provider_service.add_provider(document, descriptor)
        )
    except ProviderIdTakenError as exc:
        raise ConflictError(str(exc)) from exc
    logger.info("connectors: provider %s added for %s", descriptor.id, request.url)
    return ProviderMutationResponse.model_validate(
        {"provider": await _entry(descriptor), "fanout": applied.fanout}
    ).model_dump(mode="json")


# -- remove --------------------------------------------------------------------------------------


@operation(
    summary="Remove a connector provider added by its address",
    tags=["connectors"],
    destructive=True,
    reload_gated=True,
    errors=[ConflictError, NotFoundError, NotSupportedError],
    response_model=RemoveProviderResponse,
)
async def remove_connector_provider(provider_id: str) -> dict[str, Any]:
    """Remove a removable provider and its stored clients.

    A provider that is not removable here is a 409 naming where it is managed; a provider with a
    LIVE connection is a 409 naming the count. A connection whose session has lapsed is purged first,
    through the disconnect routine. The manifest change comes before the client rows are deleted, so a
    failed change leaves them; a failed deletion after it is raised, and running the remove again
    deletes the rows left under the id (nothing else holds it any more; no manifest change, so no
    fan-out).
    """
    _require_store()
    try:
        descriptor = connection_service.get_provider(provider_id)
    except KeyError as exc:
        if not await provider_service.clients_left_without_provider(provider_id):
            raise NotFoundError(f"provider {provider_id!r} not found") from exc
        removed = await client_store.active_client_store().delete_for_provider(provider_id)
        logger.info("connectors: %d stored client(s) left under the removed provider %s deleted", removed, provider_id)
        return RemoveProviderResponse(provider_id=provider_id, removed_clients=removed).model_dump(mode="json")
    refusal = await provider_service.removal_refusal(descriptor)
    if refusal is not None:
        raise ConflictError(refusal)
    live, lapsed = await provider_service.connections_of(provider_id)
    if live:
        raise ConflictError(
            f"provider {provider_id!r} has {len(live)} live connection(s); disconnect them before removing it"
        )
    for connection_id in lapsed:
        await connection_service.disconnect(connection_id=connection_id)
    try:
        applied = await connection_service.ConfigService.from_app().apply_change(
            lambda document: provider_service.remove_provider(document, provider_id)
        )
    except KeyError as exc:
        raise NotFoundError(f"provider {provider_id!r} is no longer in the manifest") from exc
    removed = await client_store.active_client_store().delete_for_provider(provider_id)
    logger.info("connectors: provider %s removed with %d stored client(s)", provider_id, removed)
    return RemoveProviderResponse(provider_id=provider_id, removed_clients=removed, fanout=applied.fanout).model_dump(
        mode="json"
    )


# -- set client ----------------------------------------------------------------------------------


def _client_method(
    requested: ClientAuthMethod | None, has_secret: bool, advertised: tuple[str, ...] | None
) -> ClientAuthMethod:
    """The token-endpoint authentication method the credentials are stored with.

    The requested one, else the first of ``client_secret_post`` / ``client_secret_basic`` the server
    lists when there is a secret (``client_secret_post`` when it lists none), else ``none``. Refused
    when it does not fit the secret, or when the server lists methods and not this one.
    """
    method: ClientAuthMethod
    if requested is not None:
        if (requested == "none") == has_secret:
            need = "a method other than 'none'" if has_secret else "'none'"
            raise BadRequestError(f"credentials {'with' if has_secret else 'without'} a secret need {need}")
        method = requested
    elif has_secret:
        listed: list[ClientAuthMethod] = [m for m in _SECRET_METHODS if advertised and m in advertised]
        method = listed[0] if listed else "client_secret_post"
    else:
        method = "none"
    if advertised and method not in advertised:
        raise BadRequestError(
            f"the sign-in service does not accept the authentication method {method!r}; "
            f"it lists: {', '.join(advertised)}"
        )
    return method


@operation(
    summary="Set the client credentials of a connector provider",
    tags=["connectors"],
    authority_changing=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=SetConnectorClientRequest,
    response_model=ProviderMutationResponse,
)
async def set_connector_client(
    provider_id: str,
    client_id: str,
    client_secret: str | None = None,
    token_endpoint_auth_method: ClientAuthMethod | None = None,
    issuer: str | None = None,
) -> dict[str, Any]:
    """Store the client credentials of an app registered by hand for the discovered provider ``provider_id``.

    The credentials are stored for one sign-in service the provider's server lists: ``issuer`` when
    given (400 when the server does not list it), else the one the provider's manual client is already
    stored for while the server still lists it; with neither, a 400 ``connector-client-issuer-required``
    naming the listed ones — the secret is never sent to a sign-in service nobody chose. Only a
    discovered provider that names no env client takes stored credentials.
    """
    _require_store()
    request = _validated(
        SetConnectorClientRequest,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "token_endpoint_auth_method": token_endpoint_auth_method,
            "issuer": issuer,
        },
    )
    descriptor = _provider(provider_id)
    if not descriptor.is_discovered or descriptor.client_id_env:
        raise BadRequestError(
            f"provider {provider_id!r} takes no stored client credentials: only a discovered provider "
            "with no env client does"
        )
    url = descriptor.discovery_url
    found = await _discover(url)
    listed = list(found.authorization_servers)
    store = client_store.active_client_store()
    if request.issuer is not None:
        chosen = _listed(found, request.issuer)
        if chosen is None:
            raise BadRequestError(
                f"the server does not list the sign-in service {request.issuer!r}; it lists: {', '.join(listed)}",
                extra={"issuers": listed},
            )
    else:
        stored_issuer = await store.manual_issuer(provider_id)
        chosen = None if stored_issuer is None else _listed(found, stored_issuer)
        if chosen is None:
            raise BadRequestError(
                "choose the sign-in service these credentials belong to; the server lists: " + ", ".join(listed),
                extra={"code": _ISSUER_REQUIRED_CODE, "issuers": listed},
            )
    metadata = found if discovery.same_issuer(found.issuer, chosen) else await _discover(url, bound_issuer=chosen)
    secret = request.client_secret.get_secret_value() if request.client_secret is not None else None
    method = _client_method(
        request.token_endpoint_auth_method, bool(secret), metadata.token_endpoint_auth_methods_supported
    )
    try:
        await store.put_manual(provider_id, chosen, request.client_id, secret, method)
    except ConnectorStoreNotConfiguredError as exc:
        raise NotSupportedError(str(exc), extra={"code": _NOT_CONFIGURED_CODE}) from exc
    logger.info("connectors: client credentials of provider %s set for %s (%s)", provider_id, chosen, method)
    return ProviderMutationResponse.model_validate({"provider": await _entry(descriptor), "fanout": None}).model_dump(
        mode="json"
    )
