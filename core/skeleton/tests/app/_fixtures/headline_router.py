"""A router module a reload adds: registering a ``custom_route`` on import.

Imported by the headline test's reload manifest (``routers_modules``). Each epoch
build re-imports it and re-fires the decorator into that epoch's FRESH FastMCP, so a
fresh ``http_app`` off the new core snapshots the route and actually serves it — the
reload-added-router-serves headline.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from tai42_contract.app import tai42_app

HEADLINE_PATH = "/api/headline-probe"


@tai42_app.http.custom_route(
    HEADLINE_PATH,
    methods=["GET"],
    summary="Headline probe route added by a reload.",
    tags=["test"],
    response_model=None,
    authed=False,
)
async def headline_probe(_request: Request) -> JSONResponse:
    return JSONResponse({"served": True})
