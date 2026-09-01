from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract.template import EXPRESSION_ANNOTATION_KEY, ConditionMixin, expression_annotation

# The access-control surfaces' STRICT-TRUE override of the generic condition
# payload. Unlike the truthy surfaces (hook registration, backend callbacks), the
# enforcer admits a policy/role condition ONLY when it evaluates to boolean
# ``true`` — see ``tai42_skeleton.access_control.policy.PolicyEnforcer.enforce``,
# where ``result is not True`` DENIES — so any other value, INCLUDING any other
# truthy result (a non-empty string, a number, an object), gates access off. The
# jq string evaluates over the dumped :class:`JqAuthContext`; its known top-level
# keys are glossed so an author writes against the real shape.
_ACCESS_CONDITION_ANNOTATION: dict[str, Any] = {
    EXPRESSION_ANNOTATION_KEY: expression_annotation(
        label="condition",
        blurb="the auth context (a dumped JqAuthContext)",
        keys=[
            ("sub", "the caller's subject id"),
            ("scopes", "the caller's granted scopes"),
            ("identity", "who the caller is — the token's identity claims"),
            ("policy", "the caller's static policy data"),
            ("context", "dynamic environment data for this decision"),
            ("request", "the operation being authorized"),
            ("system", "caller-supplied time/constants (e.g. the current epoch)"),
        ],
        returns="EXACTLY boolean true to allow; any other value — including any other truthy result — DENIES",
    )
}


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

    # ``condition`` is inherited from ConditionMixin but redeclared here solely to
    # refine the vendor annotation with this surface's STRICT-TRUE fact (see
    # ``_ACCESS_CONDITION_ANNOTATION``): the enforcer allows only on boolean true.
    condition: str | None = Field(default=None, json_schema_extra=_ACCESS_CONDITION_ANNOTATION)


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
    # `condition_kwargs` fields (the jq base) come from ConditionMixin; `condition`
    # is redeclared below only to carry the STRICT-TRUE annotation.
    base_tier: str | None = None

    # Same STRICT-TRUE override as AccessPolicy.condition: the enforcer admits this
    # jq base only when it evaluates to boolean true (see _ACCESS_CONDITION_ANNOTATION).
    condition: str | None = Field(default=None, json_schema_extra=_ACCESS_CONDITION_ANNOTATION)

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
