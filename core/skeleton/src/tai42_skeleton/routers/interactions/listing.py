"""Paged pending-list and parked-interactions audit door adapters for the interactions surface."""

from __future__ import annotations

from starlette.requests import Request
from tai42_contract.app import tai42_app

from tai42_skeleton.operations import (
    BadRequestError,
    operation_metadata_of,
    register_operation_route,
)
from tai42_skeleton.operations.interactions import list_interactions as _list_interactions_op
from tai42_skeleton.operations.interactions import list_pending_interactions as _list_pending_interactions_op


async def _extract_page_window(request: Request) -> dict:
    """The ``?page=`` / ``?pageSize=`` window as the list door's flat arguments.

    A GET reads its parameters from the query string, never a body. A non-integer
    is a loud 400 here; the operation range-checks the pair and caps the size.
    """
    page = request.query_params.get("page", "1")
    page_size = request.query_params.get("pageSize", "50")
    try:
        return {"page": int(page), "page_size": int(page_size)}
    except ValueError as exc:
        raise BadRequestError(f"page and pageSize must be integers: page={page!r} pageSize={page_size!r}") from exc


list_interactions = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_interactions_op),
    path="/api/interactions",
    method="GET",
    context_extractor=_extract_page_window,
    action="read",
)


async def _extract_pending_limit(request: Request) -> dict:
    """The ``?limit=`` slice as the audit door's flat argument.

    A GET reads its parameters from the query string, never a body. A non-integer
    is a loud 400 here; the operation clamps the value into range.
    """
    raw = request.query_params.get("limit")
    if raw is None:
        return {}
    try:
        return {"limit": int(raw)}
    except ValueError as exc:
        raise BadRequestError(f"limit must be an integer: limit={raw!r}") from exc


list_pending_interactions = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_pending_interactions_op),
    path="/api/interactions/pending",
    method="GET",
    context_extractor=_extract_pending_limit,
    action="read",
)
