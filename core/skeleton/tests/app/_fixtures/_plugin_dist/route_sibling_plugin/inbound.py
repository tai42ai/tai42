"""The route-carrying SIBLING: registers the plugin's HTTP route at import.

Registering at module scope models the ``@tai42_app.http.custom_route`` decorator a
real channel plugin's inbound module fires on import. The handler is defined HERE, so
the registry attributes the route to this sibling module — the module a reload must
pop+reimport to re-fire the registration.
"""

from _regprobe import register_route
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tai42_skeleton.app.route_registry import RouteOwner

OWNER = RouteOwner(kind="plugin", owner_ref="fixture/web", item_name="web")


async def inbound(request: Request) -> Response:
    """The fixture inbound handler."""
    return JSONResponse({"data": {}})


register_route("/inbound", "POST", OWNER, inbound)
