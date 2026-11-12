"""The unauthenticated served-media capability door ``/api/interactions/media/{media_id}``.

ONE capability route over the two stores :func:`~tai42_skeleton.interactions.served_media.load_served_media`
reads by the same 43-char id; this module turns its record or its refusal into the HTTP answer.
The id IS the capability secret.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.interactions import MEDIA_ID_RE, MediaKind, MediaNotFoundError, MediaStoreUnavailableError

from tai42_skeleton.app.http import http_surface
from tai42_skeleton.app.route_registry import DeclaredRouteMetadata
from tai42_skeleton.interactions.served_media import load_served_media
from tai42_skeleton.operations.storage import _content_disposition


def _reply(body: bytes, *, media_type: str, headers: dict[str, str], head: bool) -> Response:
    # A HEAD serves the same headers with no body (the plain GET path, empty payload).
    return Response(b"" if head else body, media_type=media_type, headers=headers)


def _error(message: str, status: int, *, head: bool) -> Response:
    # The plain ``{"error": ...}`` envelope on GET; a bodyless status on HEAD.
    if head:
        return Response(b"", status_code=status)
    return JSONResponse({"error": message}, status_code=status)


@http_surface().custom_route(
    "/api/interactions/media/{media_id}",
    methods=["GET", "HEAD"],
    summary="Serve interaction media stored by reference",
    tags=["interactions"],
    response_model=None,
    no_body_reason="Served interaction media: raw bytes by capability URL",
    success_media_types={"GET": ("application/octet-stream",), "HEAD": ("application/octet-stream",)},
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
    # id is a 400; every miss — a still-pending item, an absent blob, an unconfigured
    # store — answers the SAME uniform 404 (never a 501 that would oracle a store's
    # absence). The client cache is bounded by the bytes' remaining lifetime. A HEAD
    # serves the same headers, no body.
    head = request.method == "HEAD"
    media_id = request.path_params["media_id"]
    if MEDIA_ID_RE.fullmatch(media_id) is None:
        return _error("invalid media id", 400, head=head)
    try:
        record = await load_served_media(media_id, max_bytes=None)
    except MediaNotFoundError:
        return _error("not found", 404, head=head)
    except MediaStoreUnavailableError:
        return _error("media store unavailable", 503, head=head)
    headers = {
        "Cache-Control": f"private, max-age={record.expires_in}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
    }
    if record.kind is MediaKind.DOCUMENT:
        # Documents download as attachments; image/audio/video stay inline for players.
        assert record.filename is not None  # noqa: S101 — only the inbound store holds documents, each with a filename
        headers["Content-Disposition"] = _content_disposition(record.filename)
    return _reply(record.data, media_type=record.mime, headers=headers, head=head)
