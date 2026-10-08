"""The token-free-evaluability check for a background fire's policy conditions.

A fire presents no credential, so its jq context carries only the identity claim readable
from the store. A key's condition — and, when the key is owned, its OWNER's, enforced as a
second pass — must not depend on any other one. These assert that early, at the bind door;
the authoritative assertion still runs at the fire, on the text rendered then.
"""

from __future__ import annotations

from jinja2 import TemplateError
from tai42_contract.access_control.models import AccessPolicy

from tai42_skeleton.access_control.policy import render_condition
from tai42_skeleton.access_control.standing import Standing
from tai42_skeleton.authz.token_free import TokenFreeConditionError, assert_token_free_evaluable
from tai42_skeleton.template import TemplateNotFoundError


class ExecutionConditionError(TokenFreeConditionError):
    """A named principal's stored policy condition cannot be evaluated by a tokenless background execution.

    The one refusal type :func:`assert_execution_key_evaluable` and
    :class:`ExecutionKeyScan` raise. The message quotes a bounded excerpt of the
    RAW jq condition, which is store-secret; ``principal`` is a separate field so
    a door answering an untrusted caller can log the
    diagnostic and answer with the principal alone.
    """

    def __init__(self, message: str, *, principal: str) -> None:
        """Build the refusal with ``message`` and the offending ``principal``."""
        super().__init__(message)
        self.principal = principal


async def _assert_condition_evaluable(policy: AccessPolicy, *, principal: str) -> None:
    """Assert that ``policy``'s condition, rendered, can be evaluated by a tokenless background execution.

    Rendered with the identical render enforcement runs, since that text — not the stored
    template reference — is what a fire evaluates. A render failure is a loud refusal,
    NEVER read as "no condition": it would hide the very identity references being scanned
    for. Only author-fixable failures become :class:`ExecutionConditionError`; an
    infrastructure fault propagates as itself.
    """
    if policy.condition is None:
        # Nothing configured, so nothing to scan. A PRESENT-but-empty condition is NOT this
        # case — it is configured and still goes through the render.
        return
    try:
        condition = (await render_condition(policy.condition)).text
    except (ValueError, TemplateError, TemplateNotFoundError) as exc:
        raise ExecutionConditionError(
            f"the policy condition of {principal!r} does not render ({exc}), so it cannot be shown evaluable "
            "for a background execution; repair the condition before binding this execution key",
            principal=principal,
        ) from exc
    if not condition:
        return
    try:
        assert_token_free_evaluable(condition)
    except TokenFreeConditionError as exc:
        raise ExecutionConditionError(
            f"the policy condition of {principal!r} is unusable at a fire: {exc}", principal=principal
        ) from exc


async def assert_execution_key_evaluable(standing: Standing) -> None:
    """Assert that a record naming the key ``standing`` resolved can actually fire under it.

    A fire presents no credential, so its jq context carries only the identity claim
    readable from the store. The key's condition — and, when the key is owned, its
    OWNER's condition, which is enforced as a second pass — must not depend on any
    other one. Raises :class:`ExecutionConditionError` naming the offending reference —
    and the principal it belongs to — otherwise; each write surface maps that to its own
    typed refusal.

    ``standing`` is the key's, resolved by the authority assertion that must run first, so
    both conditions are the ones its single store version read and the owner is the one the
    key's stored policy names.

    Early rejection only — the authoritative assertion runs at the fire, on the text
    rendered then. Callers must already have established that access control is enabled;
    this does not check.
    """
    await _assert_condition_evaluable(standing.policy, principal=standing.principal)
    if standing.owner is not None and standing.owner_policy is not None:
        await _assert_condition_evaluable(standing.owner_policy, principal=standing.owner)
