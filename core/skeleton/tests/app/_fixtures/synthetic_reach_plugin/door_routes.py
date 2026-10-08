"""The made-up plugin's identity-introspection and pre-authentication routes."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app

_NO_BODY = "test fixture: response body not under test"


@tai42_app.http.custom_route(
    "/identity",
    methods=["GET"],
    summary="Describe the calling identity",
    tags=["synthetic"],
    response_model=None,
    no_body_reason=_NO_BODY,
    action="read",
    any_authenticated=True,
)
async def describe_identity(_request: Request) -> Response:
    """Describe the caller."""
    return JSONResponse({"data": {}})


@tai42_app.http.custom_route(
    "/entry/exchange",
    methods=["POST"],
    summary="Exchange a one-time entry code",
    tags=["synthetic"],
    response_model=None,
    no_body_reason=_NO_BODY,
    pre_auth=True,
)
async def exchange_entry_code(_request: Request) -> Response:
    """Exchange an entry code."""
    return JSONResponse({"data": {}})
