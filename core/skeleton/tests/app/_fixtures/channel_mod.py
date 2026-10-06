"""Fixture registering a channel ON IMPORT.

Loaded via a manifest ``channel_modules`` entry so each ``start()`` re-imports
it and re-runs the ``tai42_app.channels.register(...)`` side-effect — exactly as a
real channel plugin module does, including binding the plugin's own inbound route
at import. The registry is reset each ``start()``, so the repeated registration is
clean, never a duplicate-name crash.

The inbound route is registered ``authed=True``: it sits off the ``/api`` prefix (an
``/api`` fixture route would leak into the CLI-parity and OpenAPI coverage gates), and a
non-``/api`` route left public by declaration must be acknowledged at boot — so the
fixture binds an authed door, which the public-route boot audit admits without a
per-deployment acknowledgment. The channel-registration side-effect this fixture exists
to exercise is unchanged.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDelivery

from ..._helpers import DeliverOnlyChannel


class _FixtureChannel(DeliverOnlyChannel):
    async def deliver(self, delivery: ChannelDelivery) -> None:
        return None


@tai42_app.http.custom_route(
    # Deliberately OFF the ``/api/`` prefix: the route registry is process-global,
    # and an ``/api/*`` fixture route would leak into the CLI-parity and OpenAPI
    # coverage gates, which enumerate every recorded ``/api/*`` route.
    "/channels-fixture/inbound",
    methods=["POST"],
    summary="Fixture channel inbound door",
    tags=["channels"],
    response_model=None,
    no_body_reason="fixture channel webhook returns a raw provider ack, not a JSON body",
    authed=True,
    action="write",
)
async def fixture_inbound(request: Request) -> Response:
    return JSONResponse({"data": {"ok": True}})


tai42_app.channels.register("fixture_channel", _FixtureChannel())
