from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract.template import ConditionMixin


class IdentityRecord(BaseModel):
    """Schema for data stored at 'ac:key:{hash}'. Represents purely WHO the user is."""

    user_id: str

    # Extra fields (email, org_id, etc.) are treated as identity claims
    model_config = ConfigDict(extra="allow")


class AccessPolicy(ConditionMixin):
    """Schema for data stored at 'ac:policy:{user_id}'. Represents WHAT the user
    can do (permissions & logic)."""

    scopes: list[str] = Field(default_factory=list)

    # Static data required for policy decisions (e.g., {"plan_limit": 100})
    policy_data: dict[str, Any] = Field(default_factory=dict)

    # Note: 'condition' field is provided by ConditionMixin


class RoleDefinition(ConditionMixin):
    """An operator-authored role: the ONE validated shape the enforcer /
    membership-check, the management operations, and the generated Studio SDK all
    share.

    A role composes TWO layers. Layer 1 is a KEPT jq security base — carried on
    the ``condition`` field (from ``ConditionMixin``) so the body round-trips it,
    but NEVER authored through the grant map: the seed sets it (admin → ``None``,
    editor/viewer → their base jq) and a new role inherits it from its
    ``base_tier``. Layer 2 is the editable ``grants`` — a per-tag ACCESS LEVEL
    map (feature-group TAG name → ``none``/``read``/``write``) naming the role's
    level on each feature group. An absent tag means level ``none`` (deny).
    """

    name: str
    description: str

    # The scope layer; seeded roles stay ["*"] and differ only by the jq base.
    scopes: list[str] = Field(default_factory=lambda: ["*"])

    # The security posture a NEW role inherits its jq base (Layer 1) from —
    # "editor"/"viewer"; "admin" is reserved. Optional: the seeded roles carry
    # their own `condition` directly. The `condition`/`condition_id`/
    # `condition_kwargs` fields (the jq base) come from ConditionMixin.
    base_tier: str | None = None

    # The reserved/admin tier: everything, jq base None, grant map skipped at
    # enforce. Mutually exclusive with a non-empty `grants` map (see below).
    allow_all: bool = False

    # The editable Layer-2 grant map: feature-group TAG name → the role's ACCESS
    # LEVEL on that group. An absent tag is treated as `none` (deny) by the
    # enforcer; a tag explicitly mapped to `none` is a valid stored no-access.
    # An EMPTY map is a valid, fully-locked-among-grantable-surfaces role — it is
    # NOT allow-all.
    grants: dict[str, Literal["none", "read", "write"]]

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        # A stable identifier: the assignment key and the versioned store's
        # (kind, name) key component. No whitespace, no path separators.
        if not value:
            raise ValueError("role name must be a non-empty stable identifier")
        if any(char.isspace() for char in value):
            raise ValueError(f"role name must not contain whitespace: {value!r}")
        if "/" in value or "\\" in value:
            raise ValueError(f"role name must not contain path separators: {value!r}")
        return value

    @field_validator("grants")
    @classmethod
    def _check_grants(cls, value: dict[str, str]) -> dict[str, str]:
        # Only the KEYS (tag names) are checked here; each VALUE is already
        # constrained to none/read/write by the Literal type, which rejects an
        # unknown level loudly at construction.
        for tag in value:
            if not tag:
                raise ValueError("grant tag must be a non-empty feature-group name")
            if any(char.isspace() for char in tag):
                raise ValueError(f"grant tag must not contain whitespace: {tag!r}")
        return value

    @model_validator(mode="after")
    def _check_allow_all_exclusive(self) -> RoleDefinition:
        # An allow-all/admin tier grants every feature group, so it carries no
        # per-tag map — the two are mutually exclusive.
        if self.allow_all and self.grants:
            raise ValueError("an allow_all role must carry an empty grants map (mutually exclusive)")
        return self


class JqAuthContext(BaseModel):
    """The unified JSON object passed to JQ for evaluation."""

    # Standard Claims
    sub: str = "anon"
    scopes: list[str] = Field(default_factory=list)

    # IDENTITY: Who they are (mapped from AccessToken.claims)
    identity: dict[str, Any] = Field(default_factory=dict)

    # POLICY: Static rules assigned to them (from AccessPolicy)
    policy: dict[str, Any] = Field(default_factory=dict)

    # CONTEXT: Dynamic environment data (from Redis ac:context:...)
    context: dict[str, Any] = Field(default_factory=dict)

    # REQUEST: The current operation
    request: dict[str, Any] = Field(default_factory=dict)

    # SYSTEM: caller-supplied time/constants (e.g. {"time": <epoch seconds>})
    system: dict[str, float] = Field(default_factory=dict)
