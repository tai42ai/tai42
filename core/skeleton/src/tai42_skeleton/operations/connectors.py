"""Connector operations for the Studio connectors feature.

Seven operations over the connector service — the provider catalog, the installed
connections (secret-free views), and the connect/reconnect/patch/disconnect
mutations. The OAuth-completion door is NOT here: its ``{"data": {...}}`` body
carries a ``kind`` discriminator AND a load-bearing status code (a failed exchange
is HTTP 400 with a ``kind`` body), which the ``{"data": ...}`` / ``{"error": ...}``
adapter envelope cannot express — it stays a native handler in the router.

Requests parse and responses serialize THROUGH the tai42-contract connector wire
models; the mutations map a malformed body / off-list origin to a 400, an alias
collision to a 409, and an absent connection to a 404. The connect/reconnect/patch
operations take the request-derived ``redirect_uri`` / ``origin`` as flat arguments
the route's extractor computes at the HTTP edge, so the operation stays request-free.
Connection views NEVER carry tokens/config secrets — only the stored, non-secret
record fields.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from tai42_contract.connectors.errors import ConnectorError, OperatorMisconfiguredError
from tai42_contract.connectors.models import (
    AuthHealthState,
    ConnectedAccountView,
    ConnectionRecord,
    ConnectionsListResponse,
    ConnectorCategoryView,
    DisconnectResponse,
    PatchSubServicesRequest,
    PatchSubServicesResponse,
    ProviderCatalogEntry,
    ProviderCatalogResponse,
    StartConnectNoAuthResponse,
    StartConnectRequest,
    StartConnectResponse,
    StartReconnectRequest,
    SubServiceView,
)
from tai42_contract.connectors.providers import ConfigFieldSpec, ProviderDescriptor
from tai42_contract.connectors.service import (
    AliasInUseError,
    NoAuthConnectResult,
    StartConnectResult,
)

from tai42_skeleton.connectors.oauth import client as oauth_client
from tai42_skeleton.connectors.providers.registry import get_provider, list_providers
from tai42_skeleton.connectors.runtime.probe import probe
from tai42_skeleton.connectors.runtime.resolver import resolve_managed_auth
from tai42_skeleton.connectors.service import connection_service
from tai42_skeleton.connectors.settings import connectors_store_configured
from tai42_skeleton.connectors.store import token_store
from tai42_skeleton.connectors.store.catalog_store import fetch_categories
from tai42_skeleton.connectors.store.persistence import load_record_or_none
from tai42_skeleton.operations import BadRequestError, ConflictError, NotFoundError, NotSupportedError, operation

logger = logging.getLogger(__name__)

# Hard cap on the connections-list ``limit`` query argument — a guard against an
# absurd page size, not a default (an absent ``limit`` returns every connection).
_MAX_LIST_LIMIT = 500

# Upper bound on a single sub-service reachability probe (credential read + MCP
# round-trip). The GET is idempotent, so the whole probe is bounded and a timeout
# reads as unreachable — a read never blocks the caller on a slow/down provider.
# Sits above ``probe``'s own transport timeout so a live MCP round-trip completes.
_SUB_SERVICE_PROBE_TIMEOUT_SECONDS = 8.0

# The machine-readable code + message the connector START refusal carries when the
# token store is unconfigured. The read/named-entity doors answer the door's own
# 200-empty / 404 instead; only the mutation that opens a flow refuses named.
_NOT_CONFIGURED_CODE = "connectors-not-configured"
_NOT_CONFIGURED_MESSAGE = (
    "the connector token store is not configured: set CONNECTOR_STORE_PG_PASSWORD (or TAI_DEFAULT_PG_PASSWORD)"
)

# The machine-readable code a flow refusal carries when an OAuth provider is enabled
# but its operator-supplied client credentials env var is unset. Distinct from the
# store-not-configured code above: this names a per-provider credential gap, not the
# whole feature being off.
_PROVIDER_NOT_CONFIGURED_CODE = "connector-provider-not-configured"


def _not_supported_from_misconfig(exc: OperatorMisconfiguredError) -> NotSupportedError:
    """Map an operator-credential gap to a named, actionable 501.

    The provider is registered but the deployment has not supplied its OAuth client
    credentials, so this Connect flow is a capability the deployment does not
    currently provide (501), not a transient outage (503). The message names the
    offending env var; ``extra`` carries the machine-readable ``code`` and the env-var
    pointer as a structured field.
    """
    return NotSupportedError(str(exc), extra={"code": _PROVIDER_NOT_CONFIGURED_CODE, "env_var": exc.env_var})


# -- Serialization (through the contract wire models; never leaks secrets) ----


def _provider_view(provider: ProviderDescriptor) -> ProviderCatalogEntry:
    return ProviderCatalogEntry(
        id=provider.id,
        display_name=provider.display_name,
        description=provider.description,
        icon_url=provider.icon_url,
        kind=provider.kind,
        origin=provider.origin,
        category=provider.category,
        sub_services=[
            SubServiceView(id=s.id, display_name=s.display_name, description=s.description, scopes=list(s.scopes))
            for s in provider.sub_services.values()
        ],
        config_fields=[
            ConfigFieldSpec(key=f.key, label=f.label, target=f.target, required=f.required, secret=f.secret)
            for f in provider.config_fields
        ],
    )


def _connection_account(
    record: ConnectionRecord,
    *,
    unreachable_sub_services: list[str] | None = None,
) -> ConnectedAccountView:
    # Tokens (access/refresh/expiry) and no-auth config_values are SecretStr and
    # are DELIBERATELY excluded — this view is UI-facing. ``unreachable_sub_services``
    # is the live-probe result on the single-connection GET; the list is probe-free
    # and leaves it empty (its default).
    return ConnectedAccountView(
        connection_id=record.connection_id,
        provider_id=record.provider_id,
        alias=record.alias,
        kind=record.kind,
        account_identity=record.account_identity,
        enabled_sub_services=record.enabled_sub_services,
        granted_scopes=record.granted_scopes,
        auth_health_state=record.auth_health_state,
        unreachable_sub_services=unreachable_sub_services or [],
        created_at=record.created_at,
    )


# -- Reachability probing (single-connection GET only) -----------------------


async def _probe_unreachable(record: ConnectionRecord) -> list[str]:
    """Probe every enabled sub-service concurrently and return the ones that did
    not answer.

    Reachability is computed live (never stored). A connection whose provider
    plugin is no longer registered has nothing reachable, so every enabled
    sub-service is unreachable. The enabled sub-services are probed concurrently
    (each per-sub-service probe self-bounds), so the aggregate wall time stays
    near a single probe timeout rather than their sum. ``asyncio.gather``
    preserves input order, so results zip back to their sub-service names and the
    returned list keeps ``record.enabled_sub_services`` order.
    """
    try:
        descriptor = get_provider(record.provider_id)
    except KeyError:
        return list(record.enabled_sub_services)
    sub_services = list(record.enabled_sub_services)
    reachable = await asyncio.gather(
        *(_probe_sub_service(record, descriptor, sub_service) for sub_service in sub_services)
    )
    return [sub_service for sub_service, ok in zip(sub_services, reachable, strict=True) if not ok]


async def _probe_sub_service(record: ConnectionRecord, descriptor: ProviderDescriptor, sub_service: str) -> bool:
    """Whether ``sub_service`` answers a live MCP reachability probe.

    Read-only and bounded, because the single-connection GET is idempotent. OAuth
    resolves the access token WITHOUT driving a refresh (no lock, no upstream
    token exchange, no ``auth_health_state`` write, no cooldown breaker): a
    still-fresh token is probed; a token that is stale / needs reconnect / cannot
    be resolved read-only yields no credential, so the sub-service reads
    unreachable rather than triggering a refresh a read must not cause. No-auth
    injects the stored client config on the sub-service's transport channel.

    The whole per-sub-service probe (credential read + MCP round-trip) runs under
    a short timeout; a timeout, a missing provider credential
    (:class:`OperatorMisconfiguredError`), or any :class:`ConnectorError` is a
    logged, deliberate unreachable classification — never a 500.
    """
    if record.kind == "none":
        config_values = {key: value.get_secret_value() for key, value in record.config_values.items()}
        return await probe(descriptor, sub_service, config_values=config_values)
    try:
        async with asyncio.timeout(_SUB_SERVICE_PROBE_TIMEOUT_SECONDS):
            auth = await resolve_managed_auth(
                record.connection_id, record.provider_id, sub_service, allow_refresh=False
            )
            if auth is None:
                # No fresh token resolvable read-only — unreachable without an
                # upstream exchange (the read-only resolver never refreshes).
                return False
            return await probe(descriptor, sub_service, access_token=auth.access_token)
    except (ConnectorError, OperatorMisconfiguredError, TimeoutError) as exc:
        logger.info(
            "connectors: sub-service %s/%s classified unreachable on probe: %s",
            record.connection_id,
            sub_service,
            exc,
        )
        return False


def _start_result_view(result: StartConnectResult | NoAuthConnectResult) -> dict[str, Any]:
    if isinstance(result, StartConnectResult):
        return StartConnectResponse(flow_id=result.flow_id, authorize_url=result.authorize_url).model_dump(mode="json")
    return StartConnectNoAuthResponse(
        connection_id=result.connection_id,
        added_manifest_entries=result.added_manifest_entries,
        fanout=result.fanout,
    ).model_dump(mode="json")


# -- Providers + connections (reads) -----------------------------------------


@operation(summary="List connector providers", tags=["connectors"])
async def list_connector_providers() -> dict[str, Any]:
    """The provider catalog — one entry per registered connector provider, plus
    the category groupings the UI arranges them under.

    Providers come from the in-memory registry (populated by provider plugins at
    import), so they list regardless of store configuration. The category
    groupings live in the connector store's Postgres, so they are served only when
    that store is configured (otherwise an empty grouping list, mirroring the
    OFF-state connections read)."""
    providers = [_provider_view(p) for p in list_providers()]
    if connectors_store_configured():
        categories = [
            ConnectorCategoryView(id=category.id, display_name=category.display_name, sort_order=category.sort_order)
            for category in await fetch_categories()
        ]
    else:
        categories = []
    return ProviderCatalogResponse(providers=providers, categories=categories).model_dump(mode="json")


@operation(summary="List connections", tags=["connectors"], errors=[BadRequestError])
async def list_connections(health: str | None = None, limit: int | None = None) -> dict[str, Any]:
    """The installed connections as secret-free views.

    ``health`` filters the returned items to a single ``auth_health_state``
    (``healthy`` / ``reconnect_required`` / ``refresh_failing``); an unrecognised
    value is a loud 400. ``limit`` caps the number of returned items (a positive
    integer no larger than the hard guard); ``total`` still reports the full match
    count so a caller sees there is more beyond the page. ``unhealthy`` counts every
    not-healthy connection across the whole set, independent of the filter and limit.
    """
    health_filter = _parse_health_filter(health)
    page_limit = _parse_limit(limit)

    # OFF gate: with no store configured there are no connections — the honest
    # empty collection (reaching for an absent store would otherwise 500).
    if not connectors_store_configured():
        return ConnectionsListResponse(items=[], total=0, unhealthy=0).model_dump(mode="json")

    ids = await token_store().list()
    loaded = [await load_record_or_none(cid) for cid in ids]
    records = [record for record in loaded if record is not None]
    unhealthy = sum(1 for record in records if not record.is_healthy())

    matched = [record for record in records if health_filter is None or record.auth_health_state == health_filter]
    page = matched if page_limit is None else matched[:page_limit]
    items = [_connection_account(record) for record in page]
    return ConnectionsListResponse(items=items, total=len(matched), unhealthy=unhealthy).model_dump(mode="json")


def _parse_health_filter(health: str | None) -> AuthHealthState | None:
    if health is None:
        return None
    try:
        return AuthHealthState(health)
    except ValueError as exc:
        allowed = ", ".join(state.value for state in AuthHealthState)
        raise BadRequestError(f"invalid health filter {health!r}; expected one of: {allowed}") from exc


def _parse_limit(limit: int | None) -> int | None:
    if limit is None:
        return None
    if limit < 1 or limit > _MAX_LIST_LIMIT:
        raise BadRequestError(f"limit must be between 1 and {_MAX_LIST_LIMIT}")
    return limit


@operation(summary="Get a connection", tags=["connectors"], errors=[NotFoundError])
async def get_connection(connection_id: str) -> dict[str, Any]:
    """One connection's secret-free view, with live sub-service reachability; an
    unknown id is a loud 404."""
    # OFF gate: with no store no connection can exist — a 404 byte-identical to the
    # genuine miss below, so the door is no oracle for the store's absence.
    if not connectors_store_configured():
        raise NotFoundError("connection not found")
    record = await load_record_or_none(connection_id)
    if record is None:
        raise NotFoundError("connection not found")
    unreachable = await _probe_unreachable(record)
    return _connection_account(record, unreachable_sub_services=unreachable).model_dump(mode="json")


# -- Connect / reconnect / patch / disconnect (mutations) --------------------


@operation(
    summary="Start a connection flow",
    tags=["connectors"],
    destructive=True,
    errors=[BadRequestError, ConflictError, NotSupportedError],
    request_model=StartConnectRequest,
)
async def start_connect(
    provider_id: str,
    alias: str,
    enabled_sub_services: list[str],
    config_values: dict[str, str],
    return_url: str,
    redirect_uri: str,
    origin: str,
) -> dict[str, Any]:
    """Begin a Connect (OAuth authorize URL, or an immediate no-auth connection)."""
    # OFF gate: opening a flow needs the store to persist the connection — refuse
    # with a named, machine-readable reason rather than reaching for an absent store.
    if not connectors_store_configured():
        raise NotSupportedError(_NOT_CONFIGURED_MESSAGE, extra={"code": _NOT_CONFIGURED_CODE})
    try:
        result = await connection_service.start_connect(
            provider_id=provider_id,
            alias=alias,
            enabled_sub_services=enabled_sub_services,
            config_values=config_values,
            return_url=return_url,
            redirect_uri=redirect_uri,
            origin=origin,
        )
    except AliasInUseError as exc:
        raise ConflictError(str(exc)) from exc
    except OperatorMisconfiguredError as exc:
        # The provider's OAuth client credentials env var is unset — a named,
        # actionable 501, never an unnamed 500.
        raise _not_supported_from_misconfig(exc) from exc
    except (ValueError, oauth_client.OAuthError) as exc:
        # An off-list Origin surfaces as RedirectUriNotAllowedError (an OAuthError);
        # map it to a clean 400 rather than let it escape as a 500.
        raise BadRequestError(str(exc)) from exc
    return _start_result_view(result)


@operation(summary="Disconnect a connection", tags=["connectors"], errors=[NotFoundError])
async def disconnect(connection_id: str) -> dict[str, Any]:
    """Disconnect (purge) a connection; a genuinely-absent connection is a 404."""
    # OFF gate: with no store no connection can exist — a 404 byte-identical to the
    # genuine miss below.
    if not connectors_store_configured():
        raise NotFoundError("connection not found")
    # Let the service load the record with include_expired so a lapsed connection is
    # still purgeable (a serving-filtered pre-check would 404 an expired connection and
    # strand its blob + manifest entries). A genuinely-absent connection surfaces as
    # ConnectionNotFoundError.
    try:
        result = await connection_service.disconnect(connection_id=connection_id)
    except connection_service.ConnectionNotFoundError as exc:
        raise NotFoundError("connection not found") from exc
    return DisconnectResponse(
        connection_id=result.connection_id,
        upstream_revoke_outcome=result.upstream_revoke_outcome,
        upstream_revoke_status=result.upstream_revoke_status,
        removed_manifest_entries=result.removed_manifest_entries,
        fanout=result.fanout,
    ).model_dump(mode="json")


@operation(
    summary="Start a reconnect flow",
    tags=["connectors"],
    destructive=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=StartReconnectRequest,
)
async def reconnect(
    connection_id: str,
    enabled_sub_services: list[str],
    return_url: str,
    redirect_uri: str,
    origin: str,
) -> dict[str, Any]:
    """Re-run the OAuth flow for an existing connection; an unknown id is a 404."""
    # OFF gate: with no store no connection can exist — a 404 byte-identical to the
    # genuine miss below.
    if not connectors_store_configured():
        raise NotFoundError("connection not found")
    if await load_record_or_none(connection_id) is None:
        raise NotFoundError("connection not found")
    try:
        result = await connection_service.start_reconnect(
            connection_id=connection_id,
            enabled_sub_services=enabled_sub_services,
            return_url=return_url,
            redirect_uri=redirect_uri,
            origin=origin,
        )
    except OperatorMisconfiguredError as exc:
        # The provider's OAuth client credentials env var is unset — a named,
        # actionable 501, never an unnamed 500.
        raise _not_supported_from_misconfig(exc) from exc
    except (ValueError, oauth_client.OAuthError) as exc:
        # An off-list Origin surfaces as RedirectUriNotAllowedError (an OAuthError);
        # map it to a clean 400 rather than let it escape as a 500.
        raise BadRequestError(str(exc)) from exc
    return StartConnectResponse(flow_id=result.flow_id, authorize_url=result.authorize_url).model_dump(mode="json")


@operation(
    summary="Patch a connection's enabled sub-services",
    tags=["connectors"],
    destructive=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=PatchSubServicesRequest,
)
async def patch_sub_services(
    connection_id: str,
    enabled_sub_services: list[str],
    return_url: str,
    redirect_uri: str,
    origin: str,
) -> dict[str, Any]:
    """Toggle a connection's enabled sub-services; an unknown id is a 404."""
    # OFF gate: with no store no connection can exist — a 404 byte-identical to the
    # genuine miss below.
    if not connectors_store_configured():
        raise NotFoundError("connection not found")
    if await load_record_or_none(connection_id) is None:
        raise NotFoundError("connection not found")
    try:
        result = await connection_service.patch_sub_services(
            connection_id=connection_id,
            desired=enabled_sub_services,
            return_url=return_url,
            redirect_uri=redirect_uri,
            origin=origin,
        )
    except OperatorMisconfiguredError as exc:
        # The provider's OAuth client credentials env var is unset — a named,
        # actionable 501, never an unnamed 500.
        raise _not_supported_from_misconfig(exc) from exc
    except (ValueError, oauth_client.OAuthError) as exc:
        # An off-list Origin surfaces as RedirectUriNotAllowedError (an OAuthError);
        # map it to a clean 400 rather than let it escape as a 500.
        raise BadRequestError(str(exc)) from exc
    return PatchSubServicesResponse(
        connection_id=result.connection_id,
        enabled_sub_services=result.enabled_sub_services,
        consent_required=result.consent_required,
        flow_id=result.flow_id,
        authorize_url=result.authorize_url,
        added_manifest_entries=result.added_manifest_entries,
        removed_manifest_entries=result.removed_manifest_entries,
        fanout=result.fanout,
    ).model_dump(mode="json")
