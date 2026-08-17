"""The epsilon fixture's HTTP router.

Registers three routes at import via ``@tai42_app.http.custom_route``, relative to
the item's declared ``e2e-epsilon`` mount base: an authed ``GET /ping``, a public
``GET /open``, and an authed ``POST /open`` sharing the public route's path — the
per-method pin: the public method answers anonymous, its authed sibling method on
the SAME path rejects it. The skeleton installer's manifest patch persists this module
into ``routers_modules`` (before the SPA catch-all) and a process restart mounts
the routes into the served ASGI app. The returned marker payloads let a test prove
the epsilon handler answered — not the catch-all, which would 404 an ``/api/*``
path — so the routes are ordered BEFORE the catch-all. The auth posture of each
route comes from the ``tai-plugin.yml`` ``public`` flag, not a ``custom_route``
argument."""

from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app


@tai42_app.http.custom_route(
    "/ping",
    methods=["GET"],
    summary="Epsilon fixture ping",
    tags=["e2e-epsilon"],
    response_model=None,
    action="read",
)
async def epsilon_ping(request: Request) -> Response:
    """Return a fixed marker payload identifying the epsilon fixture router."""
    return JSONResponse({"data": {"epsilon": "pong", "pid": os.getpid()}})


@tai42_app.http.custom_route(
    "/open",
    methods=["GET"],
    summary="Epsilon fixture public probe",
    tags=["e2e-epsilon"],
    response_model=None,
)
async def epsilon_open(request: Request) -> Response:
    """Return a fixed marker payload from the fixture's declared-public route."""
    return JSONResponse({"data": {"epsilon": "open", "pid": os.getpid()}})


@tai42_app.http.custom_route(
    "/open",
    methods=["POST"],
    summary="Epsilon fixture authed sibling method",
    tags=["e2e-epsilon"],
    response_model=None,
    action="write",
)
async def epsilon_open_write(request: Request) -> Response:
    """Return a fixed marker from the authed POST sibling of the public GET /open."""
    return JSONResponse({"data": {"epsilon": "open-write", "pid": os.getpid()}})
