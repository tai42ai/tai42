"""Admin-only policy version-history + rollback doors."""

from __future__ import annotations

from typing import Any

from tai42_contract.versioning.errors import DocumentNotFoundError, DocumentVersionNotFoundError

import tai42_skeleton.operations.api_keys as _pkg
from tai42_skeleton.access_control import management
from tai42_skeleton.operations import BadRequestError, ForbiddenError, NotFoundError, NotSupportedError, operation
from tai42_skeleton.operations._authority import require_admin
from tai42_skeleton.operations.response_models_group_a import DocumentVersionList, PolicyRollbackResult

from .models import _DISABLED_CODE, _DISABLED_MESSAGE, PolicyRollback


@operation(
    summary="List a user's policy version history",
    tags=["access-control"],
    errors=[ForbiddenError, NotFoundError],
    response_model=DocumentVersionList,
)
async def list_policy_versions(user_id: str) -> list[dict[str, Any]]:
    """The user's append-only policy version history from the durable PG store.

    Each row is flagged ``is_current`` against the active pointer. Secret-adjacent (a version
    body carries the raw condition) and admin-only: a non-admin caller is denied 403 so it can
    never read another user's policy history. 404 when the user has no policy history.
    """
    # OFF: access control disabled → no policy history exists; the honest empty list,
    # never a store read under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        return []
    caller = await _pkg.resolve_caller()
    require_admin(caller)
    # A store-less deployment (no versioned store configured) keeps no policy version
    # history, so short-circuit an empty list rather than let the store read raw-500 on
    # an absent Postgres backend.
    from tai42_kit.db import component_store_configured

    from tai42_skeleton.db import SKELETON_COMPONENT

    if not component_store_configured(SKELETON_COMPONENT):
        return []
    try:
        versions = await _pkg.ac_policy_store().list_versions(user_id)
    except DocumentNotFoundError as exc:
        raise NotFoundError(f"no policy history for user: {user_id!r}") from exc
    return [v.model_dump() for v in versions]


@operation(
    summary="Roll a policy back to a version",
    tags=["access-control"],
    destructive=True,
    errors=[BadRequestError, ForbiddenError, NotFoundError, NotSupportedError],
    request_model=PolicyRollback,
    response_model=PolicyRollbackResult,
)
async def rollback_policy(user_id: str, version: int) -> dict[str, Any]:
    """Re-point the enforced policy to a prior version.

    Store-first: the target version body is read from the history, written to the enforced store
    (the authority) FIRST; on that success the cache-invalidation key is bumped immediately so
    enforcement follows, then the durable history pointer is advanced. Admin-only: a non-admin
    caller is denied 403 so it can never roll back another user's (or its own) enforced policy.
    404 if the version is absent or the user has no live key.
    """
    # OFF: access control disabled → refuse the rollback with a named, machine-readable
    # reason rather than operate the AC store under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        raise NotSupportedError(_DISABLED_MESSAGE, extra={"code": _DISABLED_CODE})
    caller = await _pkg.resolve_caller()
    require_admin(caller)

    # A store-less deployment (no versioned store configured) keeps no policy version
    # history, so no version can exist to roll back to — return the same clean 404 an
    # absent version yields rather than let the store read raw-500 on an absent Postgres
    # backend.
    from tai42_kit.db import component_store_configured

    from tai42_skeleton.db import SKELETON_COMPONENT

    if not component_store_configured(SKELETON_COMPONENT):
        raise NotFoundError(f"user {user_id!r} has no policy version {version}")

    store = _pkg.ac_policy_store()
    try:
        target = await store.get_version(user_id, version)
    except DocumentVersionNotFoundError as exc:
        raise NotFoundError(f"user {user_id!r} has no policy version {version}") from exc

    # Store-first, then the cache bump, then the history pointer.
    try:
        restored = await management.restore_policy_body(user_id, target.body)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if not restored:
        raise NotFoundError(f"user not found: {user_id!r}")
    await management.bump_policy_version()
    await store.rollback(user_id, version)
    return {"user_id": user_id, "active_version": version}
