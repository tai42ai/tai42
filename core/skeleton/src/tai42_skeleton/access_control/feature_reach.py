"""The acting caller's READ reach over feature tags.

A tag is readable when the gate would answer the caller's read on a route under it: the same
governing-role grant map (:func:`~tai42_skeleton.access_control.role_grants.governing_grants`)
and level rule (:func:`~tai42_skeleton.access_control.role_gate.level_satisfies`) the
per-route decision applies, over the tags a role's level can open
(:func:`~tai42_skeleton.access_control.roles.grantable_feature_tags`).
"""

from __future__ import annotations

from collections.abc import Collection

from tai42_contract.versioning.errors import DocumentNotFoundError

from tai42_skeleton.access_control.policy import policy_enforcer
from tai42_skeleton.access_control.role_gate import level_satisfies
from tai42_skeleton.access_control.role_grants import governing_grants
from tai42_skeleton.access_control.settings import access_control_settings
from tai42_skeleton.access_control.standing import STANDING_REFUSALS, StandingDenied, resolve_standing
from tai42_skeleton.access_control.user import acting_principal
from tai42_skeleton.operations.errors import PermissionDeniedError


async def readable_feature_tags(tags: Collection[str]) -> frozenset[str]:
    """The tags among ``tags`` the acting caller may READ.

    - Access control off: every tag (the HTTP edge runs no gate either).
    - An admin caller: every tag.
    - No caller bound with access control on: none (fail-closed).
    - Otherwise the caller's standing is resolved at one policy version; a principal that
      cannot carry authority raises ``PermissionDeniedError``. A role pointer naming a deleted
      role reads none (the gate denies its holders every route). A tag is readable iff a
      role's level can open it and the governing role's level on it satisfies ``read``; a
      principal no level governs reads every tag a level can open. A tag no level opens (its
      routes all admin-only, or none served by this deployment) is an admin's alone.
    """
    asked = frozenset(tags)
    settings = access_control_settings()
    if not asked or not settings.enable:
        return asked
    user_id, claims, is_admin = acting_principal()
    if is_admin:
        return asked
    if user_id is None:
        return frozenset()

    from tai42_skeleton.access_control.roles import grantable_feature_tags

    enforcer = policy_enforcer(settings)
    version = await enforcer.current_policy_version()
    try:
        standing = await resolve_standing(enforcer, user_id, version=version, verified_claims=claims)
    except StandingDenied as denied:
        raise PermissionDeniedError(STANDING_REFUSALS[denied.reason]) from denied
    try:
        grants = await governing_grants(standing.policy, standing.owner_policy, version)
    except DocumentNotFoundError:
        return frozenset()
    grantable = asked & grantable_feature_tags()
    if grants is None:
        return frozenset(grantable)
    return frozenset(tag for tag in grantable if level_satisfies(grants.get(tag, "none"), "read"))
