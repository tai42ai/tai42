"""Connector domain models + wire models.

- ``ConnectionRecord``: the plaintext payload the token store encrypts
  (one AES-GCM blob per connection: tokens + metadata + health).
- ``ConnectorRef``: back-reference written into a manifest entry marking
  it connector-managed. Tokens never live in the manifest; the runtime
  resolves them at call time keyed on ``connection_id``.
- The ``*View`` / ``*Request`` / ``*Response`` models are the wire shape
  for /api/connectors/* (narrower than the persisted record — no tokens).

Identifier validators are shared so a record and its manifest companion
agree on shape and case and can be byte-compared.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from tai42_contract.connectors.providers import SLUG_RE, ConfigFieldSpec

# Outcome of the best-effort upstream token revoke during disconnect. Shared by
# the wire model (``DisconnectResponse``) and its service twin (``DisconnectResult``).
UpstreamRevokeOutcome = Literal["success", "failed", "skipped"]

# --- Shared identifier validators ---------------------------------------

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# provider_id / sub_service use the canonical SLUG_RE imported above.
ALIAS_MAX_LEN = 32
# Alias is user-typed and baked into manifest titles + tool prefixes:
# tighter than a slug (must start alphanumeric, hyphens allowed inside).
# The UI Zod schema mirrors this regex.
ALIAS_RE = re.compile(rf"^[a-z0-9][a-z0-9_-]{{0,{ALIAS_MAX_LEN - 1}}}$")


def normalize_uuid(value: str) -> str:
    if not UUID_RE.match(value):
        raise ValueError(f"not a valid UUID: {value!r}")
    return value.lower()


def check_slug(value: str) -> str:
    if not SLUG_RE.match(value):
        raise ValueError(f"must be lowercase, start with a letter, then alphanumeric or underscore: {value!r}")
    return value


def check_alias(value: str) -> str:
    if not ALIAS_RE.match(value):
        raise ValueError(
            f"alias must be 1-{ALIAS_MAX_LEN} chars, lowercase alphanumeric "
            f"+ '-'/'_', starting with a letter or digit: {value!r}"
        )
    return value


# --- Persisted record ---------------------------------------------------


class AuthHealthState(StrEnum):
    """Connection-level auth health. Reachability is separate and computed live
    by the status probe (never stored on the record)."""

    HEALTHY = "healthy"
    RECONNECT_REQUIRED = "reconnect_required"
    REFRESH_FAILING = "refresh_failing"


class ConnectionRecord(BaseModel):
    """Plaintext payload encrypted and persisted by a ConnectorTokenStore.

    Validators run on decrypt AND on attribute assignment, so malformed
    records fail loud at the load/mutation site instead of on next decrypt.
    """

    model_config = ConfigDict(validate_assignment=True)

    connection_id: str
    provider_id: str = Field(min_length=1, max_length=64)
    alias: str

    # "oauth": OAuth-backed (tokens + identity + expiry present, no config_values).
    # "none": no-auth (tokens/identity/expiry absent; client-filled config_values).
    kind: Literal["oauth", "none"]

    # Display-only: from the OIDC id_token WITHOUT signature verification.
    # Never use as an auth key or authorization basis. None for no-auth.
    account_identity: str | None = None

    enabled_sub_services: list[str] = Field(min_length=1, max_length=64)
    granted_scopes: list[str] = Field(default_factory=list)

    # SecretStr keeps tokens out of repr/logs/tracebacks. to_storage_json()
    # is the only path that exposes the plaintext (for the encrypt step).
    # All three are None for a no-auth (kind="none") record.
    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None
    access_token_expires_at: datetime | None = None

    # No-auth only: the client-supplied static config (env values for stdio,
    # header values for http). SecretStr (like the tokens) keeps the values out
    # of repr/logs/tracebacks; to_storage_json() exposes the plaintext for the
    # encrypt step. Empty for oauth.
    config_values: dict[str, SecretStr] = Field(default_factory=dict)

    auth_health_state: AuthHealthState = AuthHealthState.HEALTHY

    created_at: datetime

    @field_validator("connection_id")
    @classmethod
    def _check_connection_id(cls, value: str) -> str:
        return normalize_uuid(value)

    @field_validator("provider_id")
    @classmethod
    def _check_provider_id(cls, value: str) -> str:
        return check_slug(value)

    @field_validator("alias")
    @classmethod
    def _check_alias(cls, value: str) -> str:
        return check_alias(value)

    @field_validator("enabled_sub_services")
    @classmethod
    def _check_sub_services(cls, value: list[str]) -> list[str]:
        return [check_slug(item) for item in value]

    @field_validator(
        "access_token_expires_at",
        "created_at",
    )
    @classmethod
    def _ensure_tz_aware(cls, value: datetime | None) -> datetime | None:
        # access_token_expires_at is None for a no-auth record; created_at is
        # always required, so its None never reaches here.
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _check_kind_invariants(self) -> ConnectionRecord:
        if self.kind == "oauth":
            if not self.access_token or not self.refresh_token:
                raise ValueError("oauth connection requires access + refresh tokens")
            if not self.account_identity:
                raise ValueError("oauth connection requires account_identity")
            if self.access_token_expires_at is None:
                raise ValueError("oauth connection requires access_token_expires_at")
            if self.config_values:
                raise ValueError("oauth connection must not carry config_values")
        else:  # kind == "none"
            if (
                self.access_token is not None
                or self.refresh_token is not None
                or self.account_identity is not None
                or self.access_token_expires_at is not None
            ):
                raise ValueError("no-auth connection must not carry tokens/identity/expiry")
        return self

    def is_healthy(self) -> bool:
        return self.auth_health_state == AuthHealthState.HEALTHY

    def to_storage_json(self) -> str:
        """Serialize with token plaintext exposed (for the encrypt step).
        Every other serializer keeps SecretStr redaction. No-auth records have
        null tokens."""
        data = self.model_dump(mode="json")
        data["access_token"] = self.access_token.get_secret_value() if self.access_token else None
        data["refresh_token"] = self.refresh_token.get_secret_value() if self.refresh_token else None
        data["config_values"] = {key: value.get_secret_value() for key, value in self.config_values.items()}
        return json.dumps(data)


class ConnectorRef(BaseModel):
    """Marker on a manifest entry pointing back at a Connection. Hand-authored
    entries leave ``managed`` as ``None`` and are unaffected."""

    connection_id: str
    provider_id: str = Field(min_length=1, max_length=64)
    sub_service: str = Field(min_length=1, max_length=64)

    @field_validator("connection_id")
    @classmethod
    def _check_uuid(cls, value: str) -> str:
        return normalize_uuid(value)

    @field_validator("provider_id", "sub_service")
    @classmethod
    def _check_slug(cls, value: str) -> str:
        return check_slug(value)


# --- Wire models (/api/connectors/*) ------------------------------------


class SubServiceView(BaseModel):
    id: str
    display_name: str
    description: str = ""
    # The OAuth scopes this sub-service requests (empty for a no-auth provider).
    scopes: list[str] = Field(default_factory=list)


class ConnectedAccountView(BaseModel):
    connection_id: str
    provider_id: str
    kind: Literal["oauth", "none"]
    alias: str
    account_identity: str | None = None
    auth_health_state: AuthHealthState
    enabled_sub_services: list[str]
    granted_scopes: list[str]
    # Sub-services whose MCP server did not answer a live reachability probe.
    # Populated only on the single-connection GET (the list is probe-free, so it
    # is always empty there). Reachability is distinct from ``auth_health_state``:
    # a healthy connection can still have an unreachable (down) sub-service.
    unreachable_sub_services: list[str] = Field(default_factory=list)
    created_at: datetime


class ProviderCatalogEntry(BaseModel):
    id: str
    display_name: str
    description: str = ""
    icon_url: str
    kind: Literal["oauth", "none"]
    # Mirrors ProviderDescriptor: who curated the provider, and the
    # connector_category id the UI groups it under.
    origin: Literal["system", "community"]
    category: str
    sub_services: list[SubServiceView]
    # No-auth only: the client-fillable config fields (empty for oauth). Drives
    # the connect-form in the UI.
    config_fields: list[ConfigFieldSpec] = Field(default_factory=list[ConfigFieldSpec])


class ConnectorCategoryView(BaseModel):
    """One ``connector_category`` grouping row — the UI's label + sort key for the
    providers it groups. Served alongside the provider catalog so clients can
    group/order/label providers by category."""

    id: str
    display_name: str
    sort_order: int


class ProviderCatalogResponse(BaseModel):
    """The provider catalog listing: the providers and the category groupings the
    UI arranges them under."""

    providers: list[ProviderCatalogEntry]
    categories: list[ConnectorCategoryView]


class ConnectionsListResponse(BaseModel):
    items: list[ConnectedAccountView]
    total: int
    # Count of connections whose ``auth_health_state`` is not HEALTHY, across the
    # whole connection set (independent of any ``health`` filter or ``limit``) — the
    # badge count the UI shows.
    unhealthy: int


class StartConnectRequest(BaseModel):
    provider_id: str
    alias: str
    enabled_sub_services: list[str] = Field(min_length=1, max_length=64)
    # No-auth only: client-filled values for the provider's config_fields.
    # Empty/omitted for oauth.
    config_values: dict[str, str] = Field(default_factory=dict)
    return_url: str = "/connectors"


class StartConnectResponse(BaseModel):
    """OAuth start: the caller opens ``authorize_url`` in a popup."""

    flow_id: str
    authorize_url: str


class StartConnectNoAuthResponse(BaseModel):
    """No-auth start: connection is created immediately, no OAuth flow."""

    connection_id: str
    added_manifest_entries: list[str] = Field(default_factory=list)
    # The awaited per-origin fleet report of the manifest broadcast this mutation
    # triggered, embedded untyped; ``None`` when no manifest mutation occurred.
    fanout: dict[str, Any] | None = None


class StartReconnectRequest(BaseModel):
    enabled_sub_services: list[str] = Field(min_length=1, max_length=64)
    return_url: str = "/connectors"


class PatchSubServicesRequest(BaseModel):
    enabled_sub_services: list[str] = Field(min_length=1, max_length=64)
    return_url: str = "/connectors"


class PatchSubServicesResponse(BaseModel):
    connection_id: str
    enabled_sub_services: list[str]
    consent_required: bool
    flow_id: str | None = None
    authorize_url: str | None = None
    added_manifest_entries: list[str] = Field(default_factory=list)
    removed_manifest_entries: list[str] = Field(default_factory=list)
    # The awaited per-origin fleet report of the manifest broadcast this mutation
    # triggered, embedded untyped; ``None`` when no manifest mutation occurred (a
    # consent-only toggle forks an OAuth flow without touching the manifest).
    fanout: dict[str, Any] | None = None


class DisconnectResponse(BaseModel):
    connection_id: str
    upstream_revoke_outcome: UpstreamRevokeOutcome
    upstream_revoke_status: int | None = None
    removed_manifest_entries: list[str]
    # The awaited per-origin fleet report of the manifest broadcast this mutation
    # triggered, embedded untyped; a disconnect always mutates the manifest.
    fanout: dict[str, Any] | None = None
