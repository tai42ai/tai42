"""The theta fixture's HTTP router.

Registers a public ``GET /{slug}`` route at import via
``@tai42_app.http.custom_route``, relative to the item's declared ``e2e-epsilon``
mount base. At its default base the resolved shape ``(e2e-epsilon, {slug})`` is a
template that overlaps epsilon's concrete ``(e2e-epsilon, ping)`` / ``(e2e-epsilon,
open)`` GET routes, so installing theta after epsilon is a route collision until
the operator remaps theta's base. The returned marker payload lets a test prove the
theta handler answered — not the catch-all, which would 404 an ``/api/*`` path — so
the route is ordered BEFORE the catch-all."""

from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app


@tai42_app.http.custom_route(
    "/{slug}",
    methods=["GET"],
    summary="Theta fixture catch route",
    tags=["e2e-theta"],
    response_model=None,
)
async def theta_catch(request: Request) -> Response:
    """Return a fixed marker payload identifying the theta fixture router."""
    return JSONResponse({"data": {"theta": request.path_params["slug"], "pid": os.getpid()}})
