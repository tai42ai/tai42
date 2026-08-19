"""The route submodule the package walk sweeps in: captures its mount base and
registers its declared route at import, exactly as a real login router does.

Reached through the ROOT package's walk under a foreign (non-route) role, so its
binding is resolved per-module from the mount map rather than inherited from the
role's absent binding. ``MOUNT_BASE`` captures ``mount_base()`` at import — the line
that raises when the walk carries no binding for this module.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app

MOUNT_BASE = tai42_app.http.mount_base()


@tai42_app.http.custom_route("/session", ["POST"], summary="login", tags=["auth"], response_model=None)
async def session(request: Request) -> Response:
    """The fixture session handler."""
    return JSONResponse({"data": {}})
