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
# The server is the sole authority on this shape: an out-of-shape alias is
# refused here (422).
ALIAS_RE = re.compile(rf"^[a-z0-9][a-z0-9_-]{{0,{ALIAS_MAX_LEN - 1}}}$")


def normalize_uuid(value: str) -> str:
    """Return ``value`` lowercased, raising ``ValueError`` when it is not a canonical UUID string."""
    if not UUID_RE.match(value):
        raise ValueError(f"not a valid UUID: {value!r}")
    return value.lower()


def check_slug(value: str) -> str:
    """Return ``value`` unchanged when it is a valid slug, else raise ``ValueError``."""
    if not SLUG_RE.match(value):
        raise ValueError(f"must be lowercase, start with a letter, then alphanumeric or underscore: {value!r}")
    return value


def check_alias(value: str) -> str:
    """Return ``value`` unchanged when it is a valid connection alias, else raise ``ValueError``."""
    if not ALIAS_RE.match(value):
        raise ValueError(
            f"alias must be 1-{ALIAS_MAX_LEN} chars, lowercase alphanumeric "
            f"+ '-'/'_', starting with a letter or digit: {value!r}"
        )
    return value


# --- Persisted record ---------------------------------------------------


class AuthHealthState(StrEnum):
    """Connection-level auth health.

    Reachability is separate and computed live by the status probe (never stored on the record).
    """

    HEALTHY = "healthy"
    RECONNECT_REQUIRED = "reconnect_required"
    REFRESH_FAILING = "refresh_failing"


class ConnectionGrant(BaseModel):
    """Where an OAuth grant of a discovered provider was obtained, so it is refreshed and revoked the same way.

    ``issuer`` is the authorization server that issued it, ``resource`` the protected-resource value
    the grant is bound to, ``client_id`` the stored client it was issued to (``None``: the env client).
    It holds no secret.
    """

    issuer: str
    resource: str | None = None
    client_id: str | None = None


class ConnectionRecord(BaseModel):
    """Plaintext payload encrypted and persisted by a ConnectorTokenStore.

    Validators run on decrypt AND on attribute assignment, so malformed
    records fail loud at the load/mutation site instead of on next decrypt.
    """

    model_config = ConfigDict(validate_assignment=True)

    connection_id: str
    provider_id: str = Field(min_length=1, max_length=64)
    alias: str

    # "oauth": OAuth-backed (access token + identity present; refresh token and
    # expiry optional; no config_values). "none": no-auth (tokens/identity/expiry
    # absent; client-filled config_values).
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

    # OAuth only. ``None``: the grant was obtained under a declared provider (refreshed with the
    # env client, no ``resource``).
    grant: ConnectionGrant | None = None
    # OAuth only: scopes a server said the token lacks; the next reconnect asks for them too.
    scopes_wanted: list[str] = Field(default_factory=list)

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
            if not self.access_token:
                raise ValueError("oauth connection requires an access token")
            if not self.account_identity:
                raise ValueError("oauth connection requires account_identity")
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
            if self.grant is not None:
                raise ValueError("no-auth connection must not carry a grant")
            if self.scopes_wanted:
                raise ValueError("no-auth connection must not carry scopes_wanted")
        return self

    def is_healthy(self) -> bool:
        """Return True when the connection's stored auth health is HEALTHY."""
        return self.auth_health_state == AuthHealthState.HEALTHY

    def to_storage_json(self) -> str:
        """Serialize with token plaintext exposed (for the encrypt step).

        Every other serializer keeps SecretStr redaction. No-auth records have null tokens.
        """
        data = self.model_dump(mode="json")
        data["access_token"] = self.access_token.get_secret_value() if self.access_token else None
        data["refresh_token"] = self.refresh_token.get_secret_value() if self.refresh_token else None
        data["config_values"] = {key: value.get_secret_value() for key, value in self.config_values.items()}
        return json.dumps(data)


class ConnectorRef(BaseModel):
    """Marker on a manifest entry pointing back at a Connection.

    Hand-authored entries leave ``managed`` as ``None`` and are unaffected.
    """

    # ``extra="forbid"``: an unknown key on a manifest entry's marker is a
    # misconfig — fail loudly naming the key rather than silently dropping it.
    model_config = ConfigDict(extra="forbid")

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


# --- Resolved facade credential -----------------------------------------


class ResolvedConnectionAuth(BaseModel):
    """The credential a connection injects, resolved for one caller.

    Returned by the ``AppConnectors.resolve_connection_auth`` facade accessor so an
    in-process plugin reads a skeleton-resolved credential without importing the
    skeleton. Carries THREE channels — an OAuth ``access_token`` plus static
    ``env`` (stdio) / ``headers`` (http) — because a connection populates whichever
    its kind uses. All values are :class:`~pydantic.SecretStr`, kept out of
    repr/logs/tracebacks. The accessor returns ``None`` when the connection injects
    nothing.
    """

    # The resolved OAuth token — the ONLY channel an OAuth resolution populates
    # (with ``env``/``headers`` empty); dropping it silently injects nothing.
    access_token: SecretStr | None = None
    # Static no-auth values merged into the request at call time: ``env`` for a
    # stdio sub-service, ``headers`` for an http one; both empty for OAuth.
    env: dict[str, SecretStr] = Field(default_factory=dict)
    headers: dict[str, SecretStr] = Field(default_factory=dict)


# --- Wire models (/api/connectors/*) ------------------------------------


class SubServiceView(BaseModel):
    """Wire view of one sub-service offered by a provider in the catalog."""

    id: str
    display_name: str
    description: str = ""
    # The OAuth scopes this sub-service requests (empty for a no-auth provider).
    scopes: list[str] = Field(default_factory=list)


class ConnectedAccountView(BaseModel):
    """Wire view of one connected account served by /api/connectors/*."""

    connection_id: str
    provider_id: str
    kind: Literal["oauth", "none"]
    alias: str
    account_identity: str | None = None
    auth_health_state: AuthHealthState
    enabled_sub_services: list[str]
    granted_scopes: list[str]
    # Scopes a server said the token lacks; the next reconnect asks for them.
    scopes_wanted: list[str] = Field(default_factory=list)
    # Sub-services whose MCP server did not answer a live reachability probe.
    # Populated only on the single-connection GET (the list is probe-free, so it
    # is always empty there). Reachability is distinct from ``auth_health_state``:
    # a healthy connection can still have an unreachable (down) sub-service.
    unreachable_sub_services: list[str] = Field(default_factory=list)
    created_at: datetime


class ProviderCatalogEntry(BaseModel):
    """Wire view of one provider offered in the connector catalog."""

    id: str
    display_name: str
    description: str = ""
    icon_url: str | None = None
    kind: Literal["oauth", "none"]
    # Mirrors ProviderDescriptor: who curated the provider, and the
    # connector_category id the UI groups it under.
    origin: Literal["system", "community"]
    category: str
    sub_services: list[SubServiceView]
    # No-auth only: the client-fillable config fields (empty for oauth). Drives
    # the connect-form in the UI.
    config_fields: list[ConfigFieldSpec] = Field(default_factory=list[ConfigFieldSpec])
    # Whether the provider door may remove this provider: a discovered provider with a stored
    # client, in the manifest's ``connectors`` list, that no installed plugin provides.
    removable: bool = False
    # Where the provider's sign-in client comes from: ``env`` (named env variables), or what the
    # client store holds — ``manual``, else a usable ``dynamic`` row, else ``unset``. ``None`` for a
    # no-auth provider.
    client_source: Literal["env", "manual", "dynamic", "unset"] | None = None


class DiscoverServerRequest(BaseModel):
    """Look up an MCP server's authorization: by its address, or the server of an existing discovered provider."""

    url: str | None = None
    provider_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> DiscoverServerRequest:
        if (self.url is None) == (self.provider_id is None):
            raise ValueError("give exactly one of url or provider_id")
        return self


class DiscoveredServerView(BaseModel):
    """What a look-up found: the server's protected resource and the sign-in service a new login would use."""

    url: str
    resource: str
    resource_name: str | None = None
    # The sign-in service a new login would use: by address the first listed; for a provider, the
    # one its logins are bound to (its manual client's issuer, else its pin, else the first listed).
    issuer: str
    # Every sign-in service the server lists, in its order.
    authorization_servers: list[str]
    # A look-up by provider only: the sign-in service its manual client credentials are stored
    # for, whether or not the server still lists it; ``None`` when it has none.
    client_issuer: str | None = None
    registration_supported: bool
    # A login started from the origin of this request needs no client credentials from anyone.
    automatic_client: bool
    scopes_supported: list[str]
    suggested_id: str
    suggested_display_name: str
    # The address an app registered by hand must allow for sign-ins started from the origin of this
    # request; ``None`` when no sign-in can start from that origin.
    redirect_uri: str | None = None


class AddServerRequest(BaseModel):
    """Add the MCP server at ``url`` as a discovered provider."""

    url: str
    id: str | None = None
    display_name: str | None = None
    description: str = ""
    icon_url: str | None = None
    category: str | None = None
    scopes: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str | None) -> str | None:
        return None if value is None else check_slug(value)


class SetConnectorClientRequest(BaseModel):
    """Store the client credentials of an app registered by hand for a discovered provider."""

    client_id: str = Field(min_length=1)
    client_secret: SecretStr | None = None
    token_endpoint_auth_method: Literal["client_secret_post", "client_secret_basic", "none"] | None = None
    # The sign-in service the credentials belong to; the secret is sent to no other.
    issuer: str | None = None


class ProviderMutationResponse(BaseModel):
    """The provider's catalog entry after a change, and the fleet report of the manifest write it made."""

    provider: ProviderCatalogEntry
    # The awaited per-origin fleet report embedded untyped; ``None`` when no manifest write occurred.
    fanout: dict[str, Any] | None = None
    # The config store's error when its exclusive write span failed to release after the
    # manifest write persisted (the change landed, and the store may still hold the span until
    # the writer process exits); ``None`` when it released or no manifest write occurred.
    span_release_error: str | None = None


class RemoveProviderResponse(BaseModel):
    """A removed provider, how many stored clients went with it, and the fleet report of the manifest write."""

    provider_id: str
    removed_clients: int
    fanout: dict[str, Any] | None = None
    # The config store's error when its exclusive write span failed to release after a manifest
    # write of the removal persisted (the change landed, and the store may still hold the span
    # until the writer process exits). The removal writes once per lapsed connection it purges and
    # once for the provider; each failed release is named, joined with "; " in write order.
    # ``None`` when every one released or no manifest write occurred.
    span_release_error: str | None = None


class ConnectorCategoryView(BaseModel):
    """One ``connector_category`` grouping row — the UI's label and sort key for its providers.

    Served alongside the provider catalog so clients can group/order/label providers by category.
    """

    id: str
    display_name: str
    sort_order: int


class ProviderCatalogResponse(BaseModel):
    """The provider catalog listing: the providers and the category groupings the UI arranges them under."""

    providers: list[ProviderCatalogEntry]
    categories: list[ConnectorCategoryView]


class ConnectionsListResponse(BaseModel):
    """Wire response listing connected accounts with the unhealthy badge count."""

    items: list[ConnectedAccountView]
    total: int
    # Count of connections whose ``auth_health_state`` is not HEALTHY, across the
    # whole connection set (independent of any ``health`` filter or ``limit``) — the
    # badge count the UI shows.
    unhealthy: int


class StartConnectRequest(BaseModel):
    """Request body to start connecting a provider."""

    provider_id: str
    alias: str
    enabled_sub_services: list[str] = Field(min_length=1, max_length=64)
    # No-auth only: client-filled values for the provider's config_fields.
    # Empty/omitted for oauth.
    config_values: dict[str, str] = Field(default_factory=dict)
    # A same-origin path to land on after consent; omitted, the deployment's declared connectors page.
    return_url: str | None = None


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
    # The config store's error when its exclusive write span failed to release after the
    # manifest write persisted (the change landed, and the store may still hold the span until
    # the writer process exits); ``None`` when it released or no manifest write occurred.
    span_release_error: str | None = None


class StartReconnectRequest(BaseModel):
    """Request body to start reconnecting an existing connection."""

    enabled_sub_services: list[str] = Field(min_length=1, max_length=64)
    # A same-origin path to land on after consent; omitted, the deployment's declared connectors page.
    return_url: str | None = None


class PatchSubServicesRequest(BaseModel):
    """Request body to change a connection's enabled sub-services."""

    enabled_sub_services: list[str] = Field(min_length=1, max_length=64)
    # A same-origin path to land on after consent; omitted, the deployment's declared connectors page.
    return_url: str | None = None


class PatchSubServicesResponse(BaseModel):
    """Wire response for a sub-service change, including any forked consent flow."""

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
    # The config store's error when its exclusive write span failed to release after the
    # manifest write persisted (the change landed, and the store may still hold the span until
    # the writer process exits); ``None`` when it released or no manifest write occurred.
    span_release_error: str | None = None


class DisconnectResponse(BaseModel):
    """Wire response for a disconnect, including the upstream revoke outcome."""

    connection_id: str
    upstream_revoke_outcome: UpstreamRevokeOutcome
    upstream_revoke_status: int | None = None
    removed_manifest_entries: list[str]
    # The awaited per-origin fleet report of the manifest broadcast this mutation
    # triggered, embedded untyped; a disconnect always mutates the manifest.
    fanout: dict[str, Any] | None = None
    # The config store's error when its exclusive write span failed to release after the
    # manifest write persisted (the change landed, and the store may still hold the span until
    # the writer process exits); ``None`` when it released or no manifest write occurred.
    span_release_error: str | None = None
