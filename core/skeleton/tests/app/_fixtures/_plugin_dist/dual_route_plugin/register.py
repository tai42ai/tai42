"""The manifest LEAF: registers its OWN route, then imports the route-carrying sibling.

The handler is defined HERE, so the registry attributes this route to the leaf module —
the leaf is one of the owner's TWO recorded route modules. Importing the sibling fires
its own registration for the side-effect; re-importing this leaf alone leaves the
sibling cached, so a reload must pop the sibling extra to re-fire it too.
"""

from _regprobe import register_route
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tai42_skeleton.app.route_registry import RouteOwner

OWNER = RouteOwner(kind="plugin", owner_ref="fixture/dual", item_name="dual")


async def status(request: Request) -> Response:
    """The fixture status handler, registered by the leaf itself."""
    return JSONResponse({"data": {}})


register_route("/status", "GET", OWNER, status)

import dual_route_plugin.inbound  # noqa: E402, F401  (route registration side-effect)
