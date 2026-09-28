"""The unauthenticated served-media capability door ``/api/interactions/media/{media_id}``.

ONE capability route, TWO stores by the same 43-char id: the inbound blob-backed store
(the metadata record on the conversations Redis → the bytes via the storage provider) is
consulted first, else the outbound Redis media store. The id IS the capability secret.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from typing import TYPE_CHECKING, cast

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.interactions import MediaKind
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.app.http import http_surface
from tai42_skeleton.app.route_registry import DeclaredRouteMetadata
from tai42_skeleton.conversations.media_meta import InboundMediaMetaStore
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.interactions.media import read_media
from tai42_skeleton.interactions.store import InteractionStore
from tai42_skeleton.operations.storage import _content_disposition

if TYPE_CHECKING:
    from tai42_contract.storage import Storage

    from tai42_skeleton.app.facets import StorageFacet

logger = logging.getLogger(__name__)

# This submodule's OWN package object, captured at import time from ``sys.modules`` — NOT
# ``from tai42_skeleton.routers import interactions``, whose parent-attribute read is stale
# mid-reload. The seam symbols are read through it at call time.
_pkg = sys.modules["tai42_skeleton.routers.interactions"]

# A stored-media id: 43 urlsafe-base64 chars (32 random bytes), the whole path
# segment. Anything else is a malformed request, answered 400 before any store read.
_MEDIA_ID_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def _blob_provider() -> Storage | None:
    """The registered blob provider off the skeleton storage facet, or ``None`` while dead."""
    return cast("StorageFacet", tai42_app.storage).provider


def _reply(body: bytes, *, media_type: str, headers: dict[str, str], head: bool) -> Response:
    # A HEAD serves the same headers with no body (the plain GET path, empty payload).
    return Response(b"" if head else body, media_type=media_type, headers=headers)


def _error(message: str, status: int, *, head: bool) -> Response:
    # The plain ``{"error": ...}`` envelope on GET; a bodyless status on HEAD.
    if head:
        return Response(b"", status_code=status)
    return JSONResponse({"error": message}, status_code=status)


async def _serve_inbound(media_id: str, *, head: bool) -> Response | None:
    """Serve the inbound blob for ``media_id``, or ``None`` when no inbound record owns it.

    A present record answers here — 200 with the blob, 404 while still pending (a capability
    id must not leak bytes before the owning message binds it), 404 when the blob has not
    landed, 503 when the record exists but the blob provider is gone. Only the absence of a
    record (or an unconfigured conversations store) falls through to the outbound store, so
    an unconfigured store never oracles its absence.
    """
    conv_settings = ConversationsSettings()
    if conv_settings.in_memory:
        return None
    store = InboundMediaMetaStore(conv_settings)
    meta = await store.get(media_id)
    if meta is None:
        return None
    if meta.pending:
        return _error("not found", 404, head=head)
    provider = _blob_provider()
    if provider is None:
        logger.warning("inbound media %s has a metadata record but no blob provider is registered", media_id)
        return _error("media store unavailable", 503, head=head)
    try:
        payload = await provider.load_bytes(meta.storage_path)
    except FileNotFoundError:
        logger.warning("inbound media %s record present but its blob %r is absent", media_id, meta.storage_path)
        return _error("not found", 404, head=head)
    score = await store.score(media_id)
    max_age = 0 if score is None else max(0, int(score - time.time()))
    headers = {
        "Cache-Control": f"private, max-age={max_age}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
    }
    if meta.kind is MediaKind.DOCUMENT:
        # Documents download as attachments; image/audio/video stay inline for players.
        headers["Content-Disposition"] = _content_disposition(meta.filename)
    return _reply(payload, media_type=meta.mime, headers=headers, head=head)


@http_surface().custom_route(
    "/api/interactions/media/{media_id}",
    methods=["GET", "HEAD"],
    summary="Serve interaction media stored by reference",
    tags=["interactions"],
    response_model=None,
    no_body_reason="Served interaction media: raw bytes by capability URL",
    authed=False,
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=False,
        error_statuses=(400, 404, 503),
        success_status=200,
    ),
)
async def media(request: Request) -> Response:
    """Serve stored interaction media by its id: 400 malformed id, 404 miss, 503 blob provider gone."""
    # UNAUTHENTICATED: the media id IS the capability secret — a vendor fetches the
    # url from its own servers, a browser ``<img>`` from the inbox origin. A malformed
    # id is a 400; the inbound blob store is consulted first (its conversations Redis is
    # configured independently of the interactions store), then the outbound Redis store;
    # a miss in both — or an unconfigured store — answers the SAME uniform 404 (never a
    # 501 that would oracle a store's absence). A HEAD serves the same headers, no body.
    head = request.method == "HEAD"
    media_id = request.path_params["media_id"]
    if _MEDIA_ID_RE.fullmatch(media_id) is None:
        return _error("invalid media id", 400, head=head)
    inbound = await _serve_inbound(media_id, head=head)
    if inbound is not None:
        return inbound
    if not _pkg.interactions_store_configured():
        return _error("not found", 404, head=head)
    settings = _pkg.interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with _pkg.client_ctx(RedisClient, settings.redis) as r:
        found = await read_media(store, r, media_id)
        if found is None:
            return _error("not found", 404, head=head)
        mime, payload = found
        # The remaining lifetime bounds the client cache: the bytes vanish at the key's
        # TTL (extended to the owning group's horizon), so a cache must not outlive it.
        remaining_ttl = await r.ttl(store.media_key(media_id))
    max_age = max(0, remaining_ttl)
    return _reply(
        payload,
        media_type=mime,
        headers={
            "Cache-Control": f"private, max-age={max_age}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
        head=head,
    )
