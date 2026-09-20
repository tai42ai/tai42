"""The route-carrying SIBLING: registers the plugin's SECOND route at import.

The handler is defined HERE, so the registry attributes this route to the sibling
module — the owner's other recorded route module. A reload must pop+reimport it under
the same binding to re-fire the registration instead of leaving it cached.
"""

from _regprobe import register_route
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tai42_skeleton.app.route_registry import RouteOwner

OWNER = RouteOwner(kind="plugin", owner_ref="fixture/dual", item_name="dual")


async def inbound(request: Request) -> Response:
    """The fixture inbound handler, registered by the sibling."""
    return JSONResponse({"data": {}})


register_route("/inbound", "POST", OWNER, inbound)
