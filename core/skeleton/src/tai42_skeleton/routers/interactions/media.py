"""The unauthenticated served-media capability door ``/api/interactions/media/{media_id}``."""

from __future__ import annotations

import re
import sys

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.app.http import http_surface
from tai42_skeleton.app.route_registry import DeclaredRouteMetadata
from tai42_skeleton.interactions.media import read_media
from tai42_skeleton.interactions.store import InteractionStore

# This submodule's OWN package object, captured at import time from ``sys.modules`` — NOT
# ``from tai42_skeleton.routers import interactions``, whose parent-attribute read is stale
# mid-reload. The seam symbols are read through it at call time.
_pkg = sys.modules["tai42_skeleton.routers.interactions"]

# A stored-media id: 43 urlsafe-base64 chars (32 random bytes), the whole path
# segment. Anything else is a malformed request, answered 400 before any store read.
_MEDIA_ID_RE = re.compile(r"[A-Za-z0-9_-]{43}")


@http_surface().custom_route(
    "/api/interactions/media/{media_id}",
    methods=["GET"],
    summary="Serve interaction media stored by reference",
    tags=["interactions"],
    response_model=None,
    no_body_reason="Served interaction media: raw bytes by capability URL",
    authed=False,
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=False,
        error_statuses=(400, 404),
        success_status=200,
    ),
)
async def media(request: Request) -> Response:
    # UNAUTHENTICATED: the media id IS the capability secret — a vendor fetches the
    # url from its own servers, a browser ``<img>`` from the inbox origin. A malformed
    # id is a 400; an unconfigured store answers the SAME uniform 404 as a miss (never
    # a 501 that would oracle the store's absence), matching the callback door.
    media_id = request.path_params["media_id"]
    if _MEDIA_ID_RE.fullmatch(media_id) is None:
        return JSONResponse({"error": "invalid media id"}, status_code=400)
    if not _pkg.interactions_store_configured():
        return JSONResponse({"error": "not found"}, status_code=404)
    settings = _pkg.interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with _pkg.client_ctx(RedisClient, settings.redis) as r:
        found = await read_media(store, r, media_id)
        if found is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        mime, payload = found
        # The remaining lifetime bounds the client cache: the bytes vanish at the key's
        # TTL (extended to the owning group's horizon), so a cache must not outlive it.
        remaining_ttl = await r.ttl(store.media_key(media_id))
    max_age = max(0, remaining_ttl)
    return Response(
        payload,
        media_type=mime,
        headers={"Cache-Control": f"private, max-age={max_age}", "X-Content-Type-Options": "nosniff"},
    )
