"""Connection lifecycle contract — start_connect, start_reconnect,
complete_connect, disconnect, patch_sub_services.

The runtime implements :class:`ConnectionService`; callers depend only on this
Protocol and the data-holder result types it returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from tai42_contract.connectors.models import UpstreamRevokeOutcome

# -- Errors ---------------------------------------------------------------


class AliasInUseError(ValueError):
    """Alias collides with an existing connection (api → 409)."""


# -- Flow operation -------------------------------------------------------


class FlowOperation(StrEnum):
    CONNECT = "connect"
    RECONNECT = "reconnect"
    TOGGLE_SUBSERVICE_ON = "toggle_subservice_on"


# -- Result types ---------------------------------------------------------


@dataclass(frozen=True)
class StartConnectResult:
    flow_id: str
    authorize_url: str


@dataclass(frozen=True)
class NoAuthConnectResult:
    """No-auth connect: the connection is created immediately (no OAuth flow)."""

    connection_id: str
    added_manifest_entries: list[str]
    # The awaited per-origin fleet report of the manifest broadcast this result's
    # write triggered, as the untyped summary the runtime embeds in the connector's
    # HTTP response; ``None`` when the operation performed no manifest mutation.
    fanout: dict[str, Any] | None = None


@dataclass(frozen=True)
class CompleteConnectResult:
    connection_id: str
    return_url: str
    operation: FlowOperation
    added_manifest_entries: list[str]
    removed_manifest_entries: list[str]
    # The awaited per-origin fleet report of the manifest broadcast this result's
    # write triggered, as the untyped summary the runtime embeds in the connector's
    # HTTP response; ``None`` when the operation performed no manifest mutation.
    fanout: dict[str, Any] | None = None


@dataclass(frozen=True)
class DisconnectResult:
    connection_id: str
    upstream_revoke_outcome: UpstreamRevokeOutcome
    upstream_revoke_status: int | None
    removed_manifest_entries: list[str]
    # The awaited per-origin fleet report of the manifest broadcast this result's
    # write triggered, as the untyped summary the runtime embeds in the connector's
    # HTTP response; ``None`` when the operation performed no manifest mutation.
    fanout: dict[str, Any] | None = None


@dataclass(frozen=True)
class PatchResult:
    connection_id: str
    enabled_sub_services: list[str]
    consent_required: bool
    flow_id: str | None
    authorize_url: str | None
    added_manifest_entries: list[str]
    removed_manifest_entries: list[str]
    # The awaited per-origin fleet report of the manifest broadcast this result's
    # write triggered, as the untyped summary the runtime embeds in the connector's
    # HTTP response; ``None`` when the operation performed no manifest mutation.
    fanout: dict[str, Any] | None = None


# -- Service protocol -----------------------------------------------------


@runtime_checkable
class ConnectionService(Protocol):
    """Connection lifecycle operations over the encrypted token store + manifest."""

    async def start_connect(
        self,
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
        provider's authorize URL. No-auth provider: validates inputs +
        config_values, creates the connection immediately and returns its id.
        """
        ...

    async def start_reconnect(
        self,
        *,
        connection_id: str,
        enabled_sub_services: list[str],
        return_url: str,
        redirect_uri: str,
        origin: str,
    ) -> StartConnectResult:
        """Re-run the OAuth flow for an existing connection (add scopes / recover
        from RECONNECT_REQUIRED)."""
        ...

    async def complete_connect(
        self,
        *,
        flow_id: str,
        code: str,
    ) -> CompleteConnectResult:
        """Exchange code for tokens, persist the encrypted ConnectionRecord, and
        write managed manifest entries. The token-exchange ``redirect_uri`` is the
        one stored in the flow state at authorize-start (byte-identical per RFC
        6749), so completion needs no ``redirect_uri`` argument."""
        ...

    async def disconnect(
        self,
        *,
        connection_id: str,
    ) -> DisconnectResult:
        """Disconnect a connection: best-effort upstream revoke, then purge the
        encrypted blob and remove managed manifest entries."""
        ...

    async def patch_sub_services(
        self,
        *,
        connection_id: str,
        desired: list[str],
        return_url: str,
        redirect_uri: str,
        origin: str,
    ) -> PatchResult:
        """Toggle which sub-services are enabled. Sub-services toggled ON whose
        scopes are not yet granted fork to a consent flow and return its
        authorize URL."""
        ...
