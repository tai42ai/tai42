"""The distribution's ``users`` router — its OWN manifest leaf and binding.

Sibling of ``routes_login`` in the SAME distribution but a DIFFERENT binding (base
``auth``). Registers at import and raises if re-imported bare, so a reload that
widened ``routes_login``'s re-import to the whole distribution would re-run THIS
module under the wrong binding (or none) and corrupt or abort it.
"""

from _regprobe import register_route
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tai42_skeleton.app.route_registry import RouteOwner

OWNER = RouteOwner(kind="plugin", owner_ref="fixture/accounts", item_name="users")


async def users(request: Request) -> Response:
    """The fixture users handler."""
    return JSONResponse({"data": {}})


register_route("/me", "GET", OWNER, users)
