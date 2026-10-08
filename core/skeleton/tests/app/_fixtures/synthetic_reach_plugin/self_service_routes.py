"""The made-up plugin's caller self-service routes, mounted under ``/api/auth``."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app

_NO_BODY = "test fixture: response body not under test"


@tai42_app.http.custom_route(
    "/synthetic/{item}",
    methods=["GET"],
    summary="Read an own synthetic item",
    tags=["synthetic"],
    response_model=None,
    no_body_reason=_NO_BODY,
    action="read",
    self_service=True,
)
async def read_own_item(_request: Request) -> Response:
    """Read the caller's own item."""
    return JSONResponse({"data": {}})


@tai42_app.http.custom_route(
    "/synthetic/{item}",
    methods=["PUT"],
    summary="Replace an own synthetic item",
    tags=["synthetic"],
    response_model=None,
    no_body_reason=_NO_BODY,
    action="write",
    self_service=True,
)
async def replace_own_item(_request: Request) -> Response:
    """Replace the caller's own item."""
    return JSONResponse({"data": {}})
