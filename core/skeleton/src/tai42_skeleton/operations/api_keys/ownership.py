"""Caller-authorization and policy-audit helpers for the api-keys surface."""

from __future__ import annotations

from typing import Any

import tai42_skeleton.operations.api_keys as _pkg
from tai42_skeleton.access_control import management
from tai42_skeleton.operations import BadRequestError, ForbiddenError, NotFoundError
from tai42_skeleton.operations._authority import Caller, owner_of

# The package this submodule belongs to; ``ac_policy_store`` is read THROUGH it at call
# time so a package-alias ``setattr`` on this generation takes effect.


def _check_scope_subset(caller: Caller, scopes: list[str]) -> None:
    """A non-admin caller may only grant scopes ⊆ its OWN current scopes (a ``"*"`` caller may grant anything).

    Raises ``BadRequestError`` naming the offending scopes.
    """
    if "*" in caller.policy.scopes:
        return
    excess = sorted(set(scopes) - set(caller.policy.scopes))
    if excess:
        raise BadRequestError(f"requested scopes exceed your own: {excess}")


async def _authorize_key_edit(caller: Caller, user_id: str, updates: dict[str, Any]) -> None:
    """Authorize an api-key edit before it is applied.

    Reads the stored body ONCE, and only when a check needs it (a non-admin ownership
    gate, or a ``policy_data`` edit whose owner claim must not change). Raises
    ``NotFoundError`` when the key is absent, ``ForbiddenError`` when the caller does not
    own it or the edit would change the immutable owner claim, and ``BadRequestError``
    when a non-admin's replacement scopes exceed its own. A no-op for an admin edit that
    does not touch ``policy_data``.
    """
    if not ((not caller.is_admin) or ("policy_data" in updates)):
        return
    stored_body = await management.get_policy_body(user_id)
    if stored_body is None:
        raise NotFoundError(f"user not found: {user_id!r}")
    stored_owner = owner_of(stored_body.get("policy_data"))
    if not caller.is_admin:
        if stored_owner != caller.caller_id:
            raise ForbiddenError("you may only edit API keys you own")
        if "scopes" in updates:
            _check_scope_subset(caller, updates["scopes"])
    if "policy_data" in updates:
        # Echo-tolerant immutability: an unchanged owner claim is accepted (Studio
        # echoes policy_data back verbatim), but a CHANGED, newly-introduced, or
        # absent/cleared owner claim is rejected — ownership never changes post-mint
        # (re-mint instead), and a silent strip would orphan the owner's visibility.
        new_owner = owner_of(updates["policy_data"])
        if new_owner != stored_owner:
            raise ForbiddenError("the owner of an API key is immutable; re-mint to change ownership")


async def _record_policy_version(user_id: str, body: dict[str, Any]) -> None:
    """Record ``body`` — the exact policy the mutation just committed to the enforced store — as version history.

    The mutation returns the body it wrote inside its own transaction, so this
    appends that precise body to the ``ac_policy`` document (create-or-append)
    without re-reading the store: two concurrent edits A→B each record their own body
    rather than both reading B and dropping A's version. A no-op when the body is
    unchanged (e.g. a description-only key edit re-writes an identical policy
    record), so history is not polluted. Any store error propagates loudly — the
    enforced store then leads the history (the safe direction: enforcement is already
    current, since the bump ran first), and the operator is told the audit write
    failed rather than it being swallowed.
    """
    await _pkg.ac_policy_store().write(user_id, body)
