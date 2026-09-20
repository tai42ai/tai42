"""The caller's own capability projection door."""

from __future__ import annotations

from typing import Any

import tai42_skeleton.operations.api_keys as _pkg
from tai42_skeleton.access_control.projection import ProjectionResult, synthetic_full_projection
from tai42_skeleton.operations import operation


@operation(
    summary="The caller's capability projection",
    tags=["access-control"],
    caller_context=True,
    response_model=ProjectionResult,
)
async def get_me(
    user_id: str | None, effective_scopes: list[str] | None, claims: dict[str, Any] | None
) -> ProjectionResult:
    """The authenticated caller's derived capability projection.

    The concrete routes, dynamic patterns, sub-MCP mounts, tools, and agents it can reach right
    now (derived, never stored).

    ``user_id``/``effective_scopes``/``claims`` are the caller's OWN identity, derived at
    the HTTP edge from the authenticated request — never caller-supplied. This is
    ``caller_context=True`` (tier-1, never projectable): as an MCP tool a caller would
    supply those identity params itself and read ANY principal's projection, so the HTTP
    route (``/api/auth/me``, whose extractor derives them from ``request.user``) is the
    only surface. With the gate OFF there is no identity to project (the edge passes
    ``user_id=None``), so a synthetic TOTAL projection is returned; otherwise the
    projection is built through the REAL enforcer so it can never advertise a door the
    gate would deny. Any infrastructure error propagates per the projection's failure
    doctrine.
    """
    if user_id is None:
        return synthetic_full_projection()
    return await _pkg.build_projection(user_id, effective_scopes or [], claims or {})
