"""Whether a principal can carry authority, and the jq passes its conditions run as — one rule for every door.

:func:`resolve_standing` is the one order every access-control door reads a principal in: its
policy exists, it is not disabled, a fire's bound key identity still holds, the owner its
STORED policy names is the owner a verified credential claims, and that owner exists and is
not disabled. Each door maps :class:`StandingDenied` onto its own wire answer.
:func:`jq_passes` builds the condition passes a request is enforced through from the same
:class:`Standing`: the principal's own, then the owner's when the owner carries a condition.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from tai42_contract.access_control import KEY_FINGERPRINT_CLAIM, OWNER_USER_ID_CLAIM
from tai42_contract.access_control.models import AccessPolicy, JqAuthContext
from tai42_contract.template import TemplatedText

from tai42_skeleton.access_control.policy import PolicyEnforcer, policy_is_empty
from tai42_skeleton.access_control.user import effective_scopes, is_admin_policy

logger = logging.getLogger(__name__)


class StandingDenyReason(StrEnum):
    """Why a principal (or its owner) cannot carry authority."""

    NO_POLICY = "no_policy"
    DISABLED = "disabled"
    FINGERPRINT_MISMATCH = "fingerprint_mismatch"
    OWNER_MISMATCH = "owner_mismatch"
    OWNER_DISABLED = "owner_disabled"
    OWNER_NO_POLICY = "owner_no_policy"


class StandingDenied(Exception):  # noqa: N818 (a refusal outcome each door maps onto its own error)
    """A principal (or its owner) cannot carry authority. ``subject`` is the user id the defect is on."""

    def __init__(self, reason: StandingDenyReason, *, principal: str, subject: str) -> None:
        """Record the ``reason``, the ``principal`` resolved, and the ``subject`` carrying the defect."""
        super().__init__(f"principal {principal!r} cannot carry authority: {reason.value} on {subject!r}")
        self.reason = reason
        self.principal = principal
        self.subject = subject


@dataclass(frozen=True)
class Standing:
    """A principal resolved for a decision: its policy, the owner its stored policy names, and what both grant."""

    principal: str
    policy: AccessPolicy
    owner: str | None
    owner_policy: AccessPolicy | None
    effective_scopes: list[str]
    is_admin: bool


def _fingerprint_matches(policy: AccessPolicy, bound_fingerprint: str) -> bool:
    """Whether the LIVE ``policy`` still carries the per-mint fingerprint a binding anchored to.

    A ``user_id`` is reusable across a revoke+remint; the fingerprint is not, so a reminted
    key never inherits an old record's authority.

    A FINGERPRINT-LESS principal (an account user — never minted, so no per-mint identity
    exists) binds with ``bound_fingerprint == ""`` and matches a live policy that carries NO
    fingerprint: there is no mint identity to anchor, and the authority checks around this
    equality (policy exists, not disabled) remain the refusal surface. Every other combination
    stays fail-closed: a MINTED key's stored fingerprint never matches ``""``, and a bound
    fingerprint never matches a policy that has since lost or changed its own. Account ids are
    never re-minted (``usr-<random>`` per create), so a fingerprint-less park cannot be
    replayed onto a recreated principal.

    A minted key whose stored ``policy_data`` fingerprint was stripped or emptied (store
    corruption, or an admin-only policy edit) reads as fingerprint-less here and binds on the
    ephemeral rebuild seam; durable fire records captured a real fingerprint at write time and
    still refuse it.
    """
    stored = policy.policy_data.get(KEY_FINGERPRINT_CLAIM)
    if bound_fingerprint == "" and stored is None:
        return True
    return stored == bound_fingerprint


async def resolve_standing(
    enforcer: PolicyEnforcer,
    user_id: str,
    *,
    version: int,
    verified_claims: Mapping[str, Any] | None,
    bound_fingerprint: str | None = None,
) -> Standing:
    """Resolve ``user_id`` at ``version``, raising :class:`StandingDenied` when it cannot carry authority.

    ``verified_claims`` are a verified credential's claims; when given, the owner they claim
    must equal the owner the stored policy names (both absent is equal). ``bound_fingerprint``
    is a fire's anchored key identity, asserted against the live policy when given. Store
    faults propagate unchanged.
    """
    policy = await enforcer.get_policy_at(user_id, version)
    if policy_is_empty(policy):
        raise StandingDenied(StandingDenyReason.NO_POLICY, principal=user_id, subject=user_id)
    if policy.policy_data.get("disabled") is True:
        raise StandingDenied(StandingDenyReason.DISABLED, principal=user_id, subject=user_id)
    if bound_fingerprint is not None and not _fingerprint_matches(policy, bound_fingerprint):
        raise StandingDenied(StandingDenyReason.FINGERPRINT_MISMATCH, principal=user_id, subject=user_id)

    owner = policy.policy_data.get(OWNER_USER_ID_CLAIM)
    if verified_claims is not None:
        claimed = verified_claims.get(OWNER_USER_ID_CLAIM)
        if claimed != owner:
            logger.error(
                "access_control: owner mismatch for %s — stored owner %r, verified credential owner %r; denying",
                user_id,
                owner,
                claimed,
            )
            raise StandingDenied(StandingDenyReason.OWNER_MISMATCH, principal=user_id, subject=user_id)

    owner_policy: AccessPolicy | None = None
    scopes = list(policy.scopes)
    if owner is not None:
        owner_policy = await enforcer.get_policy_at(owner, version)
        if owner_policy.policy_data.get("disabled") is True:
            raise StandingDenied(StandingDenyReason.OWNER_DISABLED, principal=user_id, subject=owner)
        if policy_is_empty(owner_policy):
            raise StandingDenied(StandingDenyReason.OWNER_NO_POLICY, principal=user_id, subject=owner)
        scopes = effective_scopes(policy.scopes, owner_policy.scopes)

    return Standing(
        principal=user_id,
        policy=policy,
        owner=owner,
        owner_policy=owner_policy,
        effective_scopes=scopes,
        is_admin=is_admin_policy(policy, owner_policy),
    )


@dataclass(frozen=True)
class JqPass:
    """One condition pass: whose condition it is, the condition, and the jq context minus its request."""

    principal: str
    condition: TemplatedText | None
    base: dict[str, Any]

    def context_for(self, method: str | None, canonical_path: str) -> dict[str, Any]:
        """The full jq context for one request: the base with ``.request`` set to ``method`` and ``canonical_path``."""
        return {**self.base, "request": {"method": method, "path": canonical_path}}


def jq_passes(
    standing: Standing,
    *,
    user_id: str,
    claims: Mapping[str, Any],
    live_context: dict[str, Any],
    scopes: list[str],
    now: float,
) -> list[JqPass]:
    """The condition passes a request by ``user_id`` runs, in order; each pass must admit it.

    The principal's own pass always, over ``scopes``; then the owner's pass — over the
    OWNER's policy data and scopes, so an owner condition reads the owner's policy — only
    when the owner carries a condition. Two passes are an AND; their jq is never spliced.
    """

    def _base(policy: AccessPolicy, pass_scopes: list[str]) -> dict[str, Any]:
        return JqAuthContext(
            sub=user_id,
            scopes=list(pass_scopes),
            identity=dict(claims),
            policy=policy.policy_data,
            context=live_context,
            request={},
            system={"time": now},
        ).model_dump()

    passes = [JqPass(principal=user_id, condition=standing.policy.condition, base=_base(standing.policy, scopes))]
    owner_policy = standing.owner_policy
    if standing.owner is not None and owner_policy is not None and owner_policy.condition is not None:
        passes.append(
            JqPass(
                principal=standing.owner,
                condition=owner_policy.condition,
                base=_base(owner_policy, owner_policy.scopes),
            )
        )
    return passes
