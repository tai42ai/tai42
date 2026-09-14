"""Request DTOs and the access-control-disabled refusal constants for the api-keys surface."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from tai42_contract.template import TemplatedText

# When access control is DISABLED by choice (``ACCESS_CONTROL_ENABLE=false``) the
# key-management surface must not operate the AC store under the synthetic admin:
# reads answer empty, writes refuse with this machine-readable reason. The
# ``-disabled`` suffix is DELIBERATE — the feature is off by choice, not
# unconfigured — distinct from the ``-not-configured`` codes of the store gates.
_DISABLED_CODE = "access-control-disabled"
_DISABLED_MESSAGE = "access control is disabled: set ACCESS_CONTROL_ENABLE=true to manage keys"


class ScopeUrlAdd(BaseModel):
    """Add a URL (optionally a match ``pattern``) to a scope."""

    scope_id: str
    url: str
    pattern: str | None = None


class ScopeUrlRemove(BaseModel):
    """Remove a URL from every scope that references it."""

    url: str


class ApiKeyCreate(BaseModel):
    """Create an api key for ``user_id`` with a scope set and an optional jq
    authorization ``condition`` (a templated text: inline ``content`` or a stored
    ``id``, plus its render ``kwargs``)."""

    user_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    scopes: list[str]
    policy_data: dict[str, Any] | None = None
    condition: TemplatedText | None = None
    # The account that owns this key. A non-admin caller may only mint self-owned keys
    # (an explicit different owner is rejected); an admin may set any owner or None.
    owner_user_id: str | None = None


class ApiKeyEdit(BaseModel):
    """A partial api-key edit — only the fields present are overwritten; a
    ``null``/``{}``/``""`` value clears an optional gate."""

    description: str | None = Field(default=None, min_length=1)
    scopes: list[str] | None = None
    policy_data: dict[str, Any] | None = None
    condition: TemplatedText | None = None


class KeyScopesModify(BaseModel):
    """Add and/or remove individual scopes on an api key — a granular edit that changes
    named scopes without replacing the whole set (the key ``edit`` door's ``scopes``
    field does a full replace)."""

    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class ConditionValidation(BaseModel):
    """A fail-closed jq policy-condition check — compile and (with a
    ``sample_context``) sample-evaluate a condition without persisting it."""

    condition: TemplatedText | None = None
    sample_context: dict[str, Any] | None = None


class PolicyRollback(BaseModel):
    """Re-point a user's enforced policy to a prior version."""

    version: int


class ClaimLinkCreate(BaseModel):
    """Create a one-time claim link for an existing API key. The ``api_key`` is a raw
    key the caller holds; ``ttl_seconds`` overrides the default lifetime (capped at the
    settings ceiling)."""

    api_key: str = Field(min_length=1)
    ttl_seconds: int | None = None


class PublicRoutePin(BaseModel):
    """Pin a URL public (optionally with a dynamic match ``pattern``)."""

    url: str
    pattern: str | None = None


class PublicRouteUnpin(BaseModel):
    """Unpin a public URL."""

    url: str
