"""The epsilon fixture's HTTP router — the bumped version carrying a new public route.

Registers four routes at import via ``@tai42_app.http.custom_route``, relative to
the item's declared ``e2e-epsilon`` mount base: an authed ``GET /ping``, a public
``GET /open``, an authed ``POST /open`` sharing that path (the per-method pin), and
a public ``GET /probe`` that the prior version did not declare. The auth posture of
each route comes from the ``tai-plugin.yml`` ``public`` flag, not a ``custom_route``
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


@tai42_app.http.custom_route(
    "/probe",
    methods=["GET"],
    summary="Epsilon fixture second public probe",
    tags=["e2e-epsilon"],
    response_model=None,
)
async def epsilon_probe(request: Request) -> Response:
    """Return a fixed marker payload from the public route the bumped version adds."""
    return JSONResponse({"data": {"epsilon": "probe", "pid": os.getpid()}})
