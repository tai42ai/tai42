"""The route-carrying SIBLING: records the plugin's HTTP route at import.

Recording at module scope models the ``@tai42_app.http.custom_route`` decorator a
real plugin's inbound module fires on import. The registry is read INDIRECTLY off
the module so a test can swap in a fresh ``RouteRegistry`` before driving the
reload.
"""

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tai42_skeleton.app import route_registry as _route_registry
from tai42_skeleton.app.route_registry import RouteOwner

ROUTE_PATH = "/api/channels/fixture/inbound"
ROUTE_METHOD = "POST"
OWNER = RouteOwner(kind="plugin", owner_ref="fixture/one", item_name="fixture")


async def _inbound(request: Request) -> Response:
    """The fixture inbound handler."""
    return JSONResponse({"data": {}})


_route_registry.route_registry.record(
    path=ROUTE_PATH,
    methods=[ROUTE_METHOD],
    name="fixture_inbound",
    handler=_inbound,
    summary="fixture inbound",
    tags=["fixture"],
    authed=False,
    request_model=None,
    response_model=None,
    action=None,
    owner=OWNER,
    public=True,
)
