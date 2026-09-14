"""Begin or re-begin a connection: the OAuth authorize start, the no-auth immediate
create, and the reconnect authorize start."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr
from tai42_contract.connectors.models import (
    AuthHealthState,
    ConnectionRecord,
    check_alias,
)
from tai42_contract.connectors.providers import ProviderDescriptor
from tai42_contract.connectors.service import NoAuthConnectResult, StartConnectResult

from tai42_skeleton.connectors.oauth import state
from tai42_skeleton.connectors.service import connection_service as _svc
from tai42_skeleton.connectors.service import manifest_writer

from .flow import _start_flow
from .persist import _persist
from .validation import _scopes_for, _validate_config_values, _validate_return_url, _validate_sub_services


async def start_connect(
    *,
    provider_id: str,
    alias: str,
    enabled_sub_services: list[str],
    config_values: dict[str, str] | None = None,
    return_url: str,
    redirect_uri: str,
    origin: str,
) -> StartConnectResult | NoAuthConnectResult:
    """Begin a new Connect.

    OAuth provider: validates inputs, persists OAuthFlowState, returns the
    provider's authorize URL. No-auth provider: validates inputs + config_values,
    creates the connection immediately and returns its id (no OAuth flow).

    The Origin is validated only on the OAuth branch, inside ``_start_flow``
    before it signs the redirect state — the no-auth branch has no redirect flow,
    so it is deliberately not gated on the redirect allow-list."""
    config_values = config_values or {}
    try:
        descriptor = _svc.get_provider(provider_id)
    except KeyError as exc:
        raise ValueError(f"unknown provider: {provider_id!r}") from exc

    # Alias — shared shape with ConnectionRecord + UI Zod schema. check_alias
    # already raises ValueError with a clear message on a bad shape.
    check_alias(alias)

    _validate_sub_services(descriptor, enabled_sub_services)
    _validate_return_url(return_url)

    # Alias uniqueness is enforced durably by the store's UNIQUE (provider_id,
    # alias) constraint at persist time (create-only insert → AliasInUseError); a
    # pre-check here would still race two concurrent same-alias connects.

    if descriptor.kind == "none":
        return await _connect_no_auth(
            descriptor=descriptor,
            alias=alias,
            enabled_sub_services=enabled_sub_services,
            config_values=config_values,
        )

    if config_values:
        raise ValueError(f"config_values are not accepted for oauth provider {provider_id!r}")
    return await _start_flow(
        descriptor=descriptor,
        alias=alias,
        enabled_sub_services=enabled_sub_services,
        requested_scopes=_scopes_for(descriptor, enabled_sub_services),
        return_url=return_url,
        redirect_uri=redirect_uri,
        origin=origin,
        operation=state.FlowOperation.CONNECT,
        reconnect_connection_id=None,
    )


async def _connect_no_auth(
    *,
    descriptor: ProviderDescriptor,
    alias: str,
    enabled_sub_services: list[str],
    config_values: dict[str, str],
) -> NoAuthConnectResult:
    """Create a no-auth connection: no OAuth flow, no token. Validate the client
    config, persist a minimal record, write the managed manifest entries."""
    _validate_config_values(descriptor, config_values)

    connection_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    record = ConnectionRecord(
        connection_id=connection_id,
        provider_id=descriptor.id,
        kind="none",
        alias=alias,
        enabled_sub_services=list(enabled_sub_services),
        config_values={key: SecretStr(value) for key, value in config_values.items()},
        auth_health_state=AuthHealthState.HEALTHY,
        created_at=now,
    )

    # Persist + manifest-add under the connection lock (the id is freshly
    # generated, so no contention) so a concurrent disconnect cannot delete the
    # record between the persist and the add and strand the added entries.
    added: list[str] = []

    def add_entries(document: dict[str, Any]) -> None:
        added[:] = manifest_writer.add_managed_entries(
            document,
            descriptor=descriptor,
            enabled_sub_services=enabled_sub_services,
            alias=alias,
            connection_id=connection_id,
        )

    async with _svc.connection_lock(connection_id):
        await _persist(record, create_only=True)
        # Append the managed entries through the single pipeline: it validates,
        # persists, locally reloads, and broadcasts the reload to the whole fleet.
        applied = await _svc.ConfigService.from_app().apply_change(add_entries)

    return NoAuthConnectResult(
        connection_id=connection_id,
        added_manifest_entries=added,
        fanout=applied.fanout,
    )


async def start_reconnect(
    *,
    connection_id: str,
    enabled_sub_services: list[str],
    return_url: str,
    redirect_uri: str,
    origin: str,
) -> StartConnectResult:
    """Re-run the OAuth flow for an existing connection (add scopes / recover
    from RECONNECT_REQUIRED). On callback the new tokens replace the old in
    :func:`~tai42_skeleton.connectors.service.connection_service.complete.complete_connect`.

    Reconnect is always an OAuth redirect, so the Origin is validated inside
    ``_start_flow`` before it signs the redirect state; nothing is persisted or
    mutated before that call."""
    record = await _svc.load_record(connection_id)
    if record.kind == "none":
        raise ValueError(f"no-auth connection {connection_id} cannot be reconnected")
    try:
        descriptor = _svc.get_provider(record.provider_id)
    except KeyError as exc:
        # The provider plugin was removed since this connection was created; surface
        # a typed ValueError the router maps to a 4xx instead of a raw KeyError 500.
        raise ValueError(f"unknown provider: {record.provider_id!r}") from exc

    _validate_sub_services(descriptor, enabled_sub_services)
    _validate_return_url(return_url)

    return await _start_flow(
        descriptor=descriptor,
        alias=record.alias,
        enabled_sub_services=enabled_sub_services,
        requested_scopes=_scopes_for(descriptor, enabled_sub_services),
        return_url=return_url,
        redirect_uri=redirect_uri,
        origin=origin,
        operation=state.FlowOperation.RECONNECT,
        reconnect_connection_id=connection_id,
    )
