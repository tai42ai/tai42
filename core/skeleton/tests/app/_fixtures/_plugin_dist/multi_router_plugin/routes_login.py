"""The distribution's ``login`` router — its OWN manifest leaf and binding.

Registers its route at import (resolving its mount base through the bound binding),
so re-importing it bare — with no binding bound — raises. A reload must re-fire it
under the ``login`` binding alone.
"""

from _regprobe import register_route
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tai42_skeleton.app.route_registry import RouteOwner

OWNER = RouteOwner(kind="plugin", owner_ref="fixture/accounts", item_name="login")


async def login(request: Request) -> Response:
    """The fixture login handler."""
    return JSONResponse({"data": {}})


register_route("/session", "POST", OWNER, login)
