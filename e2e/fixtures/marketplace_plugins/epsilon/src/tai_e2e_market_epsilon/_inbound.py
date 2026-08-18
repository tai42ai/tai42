"""The epsilon fixture's route registrations — the route-carrying SIBLING.

The manifest leaf ``tai_e2e_market_epsilon.router`` registers by importing THIS
module, so the ``@tai42_app.http.custom_route`` decorators fire purely as an import
side-effect of a SIBLING (the web/whatsapp/slack channel shape), not from the leaf
itself. This is what makes an in-process config reload exercise the sibling re-fire:
re-importing only the leaf leaves this module cached, so the reload must pop+reimport
this sibling under the router's binding to re-register the routes.

Three routes relative to the item's declared ``e2e-epsilon`` mount base: an authed
``GET /ping``, a public ``GET /open``, and an authed ``POST /open`` sharing the
public route's path (the per-method pin). The auth posture of each route comes from
the ``tai-plugin.yml`` ``public`` flag, not a ``custom_route`` argument.
"""

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
