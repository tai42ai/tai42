"""The unauthenticated mid-form reaction door of a ticketed callback form.

Serves ``/api/interactions/callback/{ticket}/react``: a sibling of the answer callback
door under the SAME ticket capability and the SAME size caps. It runs the one reaction
chokepoint and returns the validated form update. STATELESS — nothing durable is recorded.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response
from tai42_contract.interactions import FormReactionClosedError, FormReactionHandlerError, FormReactionRequestError
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.app.http import http_surface
from tai42_skeleton.app.route_registry import DeclaredRouteMetadata
from tai42_skeleton.interactions.reaction import react
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.middleware.rate_limit import SKELETON_DOOR_BUDGET
from tai42_skeleton.operations.adapter import parse_json_object
from tai42_skeleton.operations.errors import BadRequestError

from .callback_envelope import callback_json, callback_post_size_guard

logger = logging.getLogger(__name__)

# This submodule's OWN package object, captured at import time from ``sys.modules`` — NOT
# ``from tai42_skeleton.routers import interactions``, whose parent-attribute read is stale
# mid-reload. The seam symbols (``client_ctx``/``interactions_settings``/
# ``interactions_store_configured``) are read through it at call time.
_pkg = sys.modules["tai42_skeleton.routers.interactions"]


def _parse_react_body(raw: bytes) -> Response | tuple[Any, Any]:
    """Parse a mid-form reaction body into ``(event, values)``, or a 400 ``Response``.

    The reacting channel forwards ``{"event": {...}, "values": {...}}`` — the triggering
    event and the values filled so far. A malformed or incomplete body is a 400.
    """
    if not raw:
        return callback_json({"error": "body must contain 'event' and 'values'"}, 400)
    try:
        parsed = parse_json_object(raw)
    except BadRequestError as exc:
        return callback_json({"error": exc.message}, 400)
    if "event" not in parsed or "values" not in parsed:
        return callback_json({"error": "body must contain 'event' and 'values'"}, 400)
    return parsed["event"], parsed["values"]


async def _callback_react(
    request: Request, r: Any, store: InteractionStore, settings: InteractionsSettings, raw: bytes
) -> Response:
    """The mid-form reaction claim for a ticketed channel-delivered reacting form.

    A SIBLING of the answer callback door under the SAME ticket (the ticket is the
    capability, exactly as for the answer claim) and the SAME size caps: resolve the ticket,
    parse the event + partial values, and run the ONE ``react`` chokepoint. STATELESS — no
    answer is recorded and the durable record is untouched. A handler failure/deadline is a
    loud 502 (never a stale/silent value). ``raw`` is the body the caller's size guard read.
    """
    ticket = request.path_params["ticket"]
    interaction_id = await store.resolve_ticket(r, ticket)
    if interaction_id is None:
        return callback_json({"error": "not found"}, 404)
    parsed = _parse_react_body(raw)
    if isinstance(parsed, Response):
        return parsed
    event, values = parsed
    try:
        update = await react(interaction_id, event, values)
    except FormReactionClosedError as exc:
        return callback_json({"error": exc.reason}, 409)
    except FormReactionRequestError as exc:
        return callback_json({"error": exc.reason}, 400)
    except FormReactionHandlerError as exc:
        logger.exception("reaction callback: handler failed for interaction %s", interaction_id)
        return callback_json({"error": exc.reason}, 502)
    return callback_json({"data": {"update": update}}, 200)


class InteractionReactionAck(BaseModel):
    """The reaction door's JSON body: the validated form ``update`` a reaction returned."""

    update: dict[str, Any]


@http_surface().custom_route(
    "/api/interactions/callback/{ticket}/react",
    methods=["POST"],
    summary="External interaction mid-form reaction door",
    tags=["interactions"],
    response_model=InteractionReactionAck,
    authed=False,
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=False,
        error_statuses=(400, 404, 409, 413, 502),
        success_status=200,
        rate_limit_budget=SKELETON_DOOR_BUDGET,
    ),
)
async def callback_react(request: Request) -> Response:
    """Serve the unauthenticated mid-form reaction door (POST only), under the ticket capability."""
    settings = _pkg.interactions_settings()
    store = InteractionStore(settings.key_prefix)
    # OFF gate — same uniform-404 (after the shared size guard) the answer callback door gives,
    # so an unconfigured store is no oracle on this public door.
    if not _pkg.interactions_store_configured():
        oversized = await callback_post_size_guard(request, settings)
        if isinstance(oversized, Response):
            return oversized
        return callback_json({"error": "not found"}, 404)
    async with _pkg.client_ctx(RedisClient, settings.redis) as r:
        # Size caps (query string + actual body bytes) come first — the SAME guard the OFF
        # gate runs, so an oversized POST answers 413 in both states.
        guarded = await callback_post_size_guard(request, settings)
        if isinstance(guarded, Response):
            return guarded
        return await _callback_react(request, r, store, settings, guarded)
