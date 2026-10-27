"""Response models for the API-key operations.

Scopes, route mappings, keys, mint capabilities and roles, conditions, and policy history.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, RootModel
from tai42_contract.access_control.models import AccessCondition, RoleDefinition

from tai42_skeleton.access_control.projection import PrincipalRef
from tai42_skeleton.access_control.role_gate import RoleReach
from tai42_skeleton.app.route_registry import RouteAction


class ScopeUrlMap(RootModel[dict[str, str]]):
    """The scope catalog as a ``{url: scope_id}`` mapping; empty when access control is disabled."""


class ScopeUrlAck(BaseModel):
    """Acknowledges a url mapped onto a scope."""

    scope_id: str
    url: str


class UrlAck(BaseModel):
    """Acknowledges a url-scoped mutation (scope-url removal, public-route pin/unpin)."""

    url: str


class ScopeDeleteResult(BaseModel):
    """A deleted scope with the number of keys the cascade rewrote."""

    scope_id: str
    deleted_keys: int


class RouteMappingRow(BaseModel):
    """One HTTP route joined with its scope mapping and route-registry metadata.

    ``mapped`` is the route's scope id, the public marker, or ``null`` when unmapped;
    ``action`` is its authorization action class, ``null`` when the route carries no
    registry metadata; ``role_reach`` is the server's decision of what opens the route for a
    non-admin role (``level``: a role's per-tag level; ``self_service``: every role keeps it,
    no level; ``admin_only``: a fenced/secret class or a control-plane route the base tier
    reserves; ``public``: the route is not authenticated and answers everyone without a
    login), ``null`` when the route carries no registry metadata; ``declared_public`` is
    ``True`` when the route is public by its own declaration and so answers everyone whatever
    scope it is mapped to.
    """

    path: str
    methods: list[str]
    mapped: str | None
    tags: list[str]
    summary: str
    action: RouteAction | None
    role_reach: RoleReach | None
    declared_public: bool


class RouteMappingList(RootModel[list[RouteMappingRow]]):
    """The app's HTTP routes with their scope mappings, sorted by path."""


class TokenPayloadRow(BaseModel):
    """A provisioned key's identity merged with its enforced policy — NEVER key material.

    The policy fields are present only when the key carries a stored policy row; the owner claim, when set,
    rides inside ``policy_data``. ``principal`` is the key's OWNER principal (kind + display name), ``null``
    only for a key whose principal row is absent (an orphaned restore state). ``orphaned`` is ``True`` for a
    policy row minted as an api key whose identity record is gone (a partial-restore state): it authenticates
    nothing until re-minted from the backup.
    """

    user_id: str
    description: str
    scopes: list[str] | None = None
    policy_data: dict[str, Any] | None = None
    condition: AccessCondition = None
    principal: PrincipalRef | None = None
    orphaned: bool = False


class TokenPayloadList(RootModel[list[TokenPayloadRow]]):
    """Every provisioned key's identity + policy the caller may see."""


class ApiKeyCreateResult(BaseModel):
    """A freshly minted key.

    ``api_key`` is the raw ``sk-…`` secret surfaced ONCE — it is never stored in plaintext and never
    returned again; ``key_fingerprint`` is the key's immutable per-mint identity a binding pins against.
    """

    api_key: str
    key_fingerprint: str


class UserUpdateAck(BaseModel):
    """Acknowledges an api-key edit."""

    user_id: str
    updated: bool


class ScopesUpdateAck(BaseModel):
    """Acknowledges a granular scope edit, returning the resulting scope set."""

    user_id: str
    updated: bool
    scopes: list[str]


class RevokeAck(BaseModel):
    """Acknowledges an api-key revocation."""

    user_id: str
    revoked: bool


class ClaimLinkResult(BaseModel):
    """A one-time claim link.

    ``token`` is the claim secret surfaced ONCE (it rides the ``claim_path`` URL fragment and is never
    stored in plaintext); ``expires_at`` is its ISO-8601 expiry.
    """

    claim_path: str
    token: str
    expires_at: str


class ProviderCapability(BaseModel):
    """Whether one configured identity provider can mint keys."""

    name: str
    mintable: bool


class MintCapabilities(BaseModel):
    """Whether any configured identity provider can mint keys, per provider."""

    mintable: bool
    providers: list[ProviderCapability]


class RoleDefinitionList(RootModel[list[RoleDefinition]]):
    """The seeded / operator-authored roles as full role bodies."""


class ConditionCheckResult(BaseModel):
    """A jq policy-condition validation verdict.

    ``result`` is the sampled allow/deny boolean, or ``null`` when no sample context was evaluated.
    """

    ok: bool
    result: bool | None


class PolicyRollbackResult(BaseModel):
    """The version a policy was rolled back to."""

    user_id: str
    active_version: int
