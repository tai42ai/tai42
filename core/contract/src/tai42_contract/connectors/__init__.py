"""Connectors contract — pure interfaces and wire/domain models.

No runtime behaviour lives here: only the token-store ABC, the connection
service Protocol, provider descriptors, domain + wire models, probe result
models, and the connector error types.
"""

from __future__ import annotations

from tai42_contract.connectors.errors import (
    ConnectorError,
    OperatorMisconfiguredError,
)
from tai42_contract.connectors.models import (
    ALIAS_MAX_LEN,
    ALIAS_RE,
    SLUG_RE,
    UUID_RE,
    AuthHealthState,
    ConnectedAccountView,
    ConnectionRecord,
    ConnectionsListResponse,
    ConnectorRef,
    DisconnectResponse,
    PatchSubServicesRequest,
    PatchSubServicesResponse,
    ProviderCatalogEntry,
    StartConnectNoAuthResponse,
    StartConnectRequest,
    StartConnectResponse,
    StartReconnectRequest,
    SubServiceView,
    check_alias,
    check_slug,
    normalize_uuid,
)
from tai42_contract.connectors.probe import ToolSummary, VerifyResult
from tai42_contract.connectors.providers import (
    ConfigFieldSpec,
    McpServerDescriptor,
    OAuthEndpoints,
    ProviderDescriptor,
    SubServiceDescriptor,
)
from tai42_contract.connectors.service import (
    AliasInUseError,
    CompleteConnectResult,
    ConnectionService,
    DisconnectResult,
    FlowOperation,
    NoAuthConnectResult,
    PatchResult,
    StartConnectResult,
)
from tai42_contract.connectors.store import ConnectorTokenStore

__all__ = [
    "ALIAS_MAX_LEN",
    "ALIAS_RE",
    "SLUG_RE",
    "UUID_RE",
    "AliasInUseError",
    "AuthHealthState",
    "CompleteConnectResult",
    "ConfigFieldSpec",
    "ConnectedAccountView",
    "ConnectionRecord",
    "ConnectionService",
    "ConnectionsListResponse",
    "ConnectorError",
    "ConnectorRef",
    "ConnectorTokenStore",
    "DisconnectResponse",
    "DisconnectResult",
    "FlowOperation",
    "McpServerDescriptor",
    "NoAuthConnectResult",
    "OAuthEndpoints",
    "OperatorMisconfiguredError",
    "PatchResult",
    "PatchSubServicesRequest",
    "PatchSubServicesResponse",
    "ProviderCatalogEntry",
    "ProviderDescriptor",
    "StartConnectNoAuthResponse",
    "StartConnectRequest",
    "StartConnectResponse",
    "StartConnectResult",
    "StartReconnectRequest",
    "SubServiceDescriptor",
    "SubServiceView",
    "ToolSummary",
    "VerifyResult",
    "check_alias",
    "check_slug",
    "normalize_uuid",
]
