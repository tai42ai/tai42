"""The epsilon fixture's HTTP router.

Registers a distinctive authed GET route at import via
``@tai42_app.http.custom_route``, so the skeleton installer's manifest patch
persists this module into ``routers_modules`` (before the SPA catch-all) and a
process restart mounts the route into the served ASGI app. The returned marker
payload lets a test prove the epsilon handler answered — not the catch-all, which
would 404 an ``/api/*`` path — so the route is ordered BEFORE the catch-all."""

from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app


@tai42_app.http.custom_route(
    "/api/e2e-epsilon/ping",
    methods=["GET"],
    summary="Epsilon fixture ping",
    tags=["e2e-epsilon"],
    response_model=None,
    authed=True,
    action="read",
)
async def epsilon_ping(request: Request) -> Response:
    """Return a fixed marker payload identifying the epsilon fixture router."""
    return JSONResponse({"data": {"epsilon": "pong", "pid": os.getpid()}})
