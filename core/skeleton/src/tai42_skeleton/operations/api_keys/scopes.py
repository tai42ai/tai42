"""Scope catalog + CRUD doors: list scopes, add/remove a scope url, delete a scope."""

from __future__ import annotations

from typing import Any

import tai42_skeleton.operations.api_keys as _pkg
from tai42_skeleton.access_control import management
from tai42_skeleton.operations import BadRequestError, NotFoundError, NotSupportedError, operation
from tai42_skeleton.operations.response_models_group_a import (
    ScopeDeleteResult,
    ScopeUrlAck,
    ScopeUrlMap,
    UrlAck,
)

from .models import _DISABLED_CODE, _DISABLED_MESSAGE, ScopeUrlAdd, ScopeUrlRemove


@operation(summary="List all scopes", tags=["access-control"], response_model=ScopeUrlMap)
async def list_scopes() -> dict[str, str]:
    """Every non-public route mapping as ``{url: scope_id}``."""
    # OFF: access control disabled → the honest empty mapping, no store touched.
    if not _pkg.access_control_settings().enable:
        return {}
    return await management.get_all_existing_scopes()


@operation(
    summary="Add a URL to a scope",
    tags=["access-control"],
    destructive=True,
    errors=[BadRequestError, NotSupportedError],
    request_model=ScopeUrlAdd,
    response_model=ScopeUrlAck,
)
async def add_scope_url(scope_id: str, url: str, pattern: str | None) -> dict[str, str]:
    """Map ``url`` to ``scope_id`` (optionally with a dynamic match ``pattern``)."""
    # OFF: access control disabled → refuse the write with a named, machine-readable
    # reason rather than operate the AC store under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        raise NotSupportedError(_DISABLED_MESSAGE, extra={"code": _DISABLED_CODE})
    marker = _pkg.access_control_settings().public_resource_id
    if scope_id == marker:
        # The public marker is a column value, not a scope. Routing it through the
        # generic scope setter would write a public pin behind the scope machinery's
        # back; the dedicated public-routes door is the only public-pin writer.
        raise BadRequestError(f"{marker!r} is the public marker, not a scope; use POST /api/auth/public-routes")
    try:
        await management.add_url_to_scope(scope_id, url, pattern)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    await management.bump_policy_version()
    return {"scope_id": scope_id, "url": url}


@operation(
    summary="Remove a URL from all scopes",
    tags=["access-control"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=ScopeUrlRemove,
    response_model=UrlAck,
)
async def remove_scope_url(url: str) -> dict[str, str]:
    """Unmap ``url`` from every scope that references it; a url that was never mapped
    is a loud 404 (a typo, not a silent success)."""
    # OFF: access control disabled → refuse the write with a named, machine-readable
    # reason rather than operate the AC store under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        raise NotSupportedError(_DISABLED_MESSAGE, extra={"code": _DISABLED_CODE})
    existed, affected = await management.remove_url_from_scope(url)
    if not existed:
        raise NotFoundError(f"url not mapped: {url!r}")
    # The store cascade has landed; bump the cache-buster first so enforcement follows
    # immediately, then record each rewritten policy as a new PG version so the durable
    # history's ``is_current`` stays honest against enforcement (a rollback target that
    # still listed the removed scope would silently re-grant it).
    await management.bump_policy_version()
    for affected_user, body in affected:
        await _pkg._record_policy_version(affected_user, body)
    return {"url": url}


@operation(
    summary="Delete a scope",
    tags=["access-control"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    response_model=ScopeDeleteResult,
)
async def delete_scope(scope_id: str) -> dict[str, Any]:
    """Delete a scope, cascading it out of every referencing key; an unknown scope
    (no urls) is a loud 404."""
    # OFF: access control disabled → refuse the delete with a named, machine-readable
    # reason rather than operate the AC store under the synthetic admin.
    if not _pkg.access_control_settings().enable:
        raise NotSupportedError(_DISABLED_MESSAGE, extra={"code": _DISABLED_CODE})
    try:
        deleted, affected = await management.remove_scope(scope_id)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if deleted == 0:
        raise NotFoundError(f"scope not found: {scope_id!r}")
    # The delete cascades the scope out of every referencing key's stored policy; bump
    # the cache-buster first so enforcement follows immediately, then record each
    # rewritten policy as a new PG version so the durable history's ``is_current`` stays
    # honest against enforcement.
    await management.bump_policy_version()
    for affected_user, body in affected:
        await _pkg._record_policy_version(affected_user, body)
    return {"scope_id": scope_id, "deleted_keys": deleted}
