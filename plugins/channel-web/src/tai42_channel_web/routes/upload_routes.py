"""The upload door: one multipart file per request, ingested PENDING through the seam.

A visitor uploads a file AHEAD of the message that will reference it. The bytes stream
through the ONE ``app.media`` ingest chokepoint with no owning message
(``origin.message_id=None``), so the item is held PENDING under the seam's pending TTL
until the send binds it (``routes/message_routes.py``). The door returns the minted
served-media id + same-origin reference; every ingest failure is a loud typed
``{error, code}`` refusal, never a silent drop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from pydantic import ValidationError
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import Response
from tai42_contract.app import DeclaredRouteMetadata, tai42_app
from tai42_contract.interactions import (
    IngestedMedia,
    MediaOrigin,
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)
from tai42_kit.interactions import media_ingest_cap_settings

from tai42_channel_web.routes.dtos import IdentityBody, UploadAcceptedResponse
from tai42_channel_web.routes.envelope import _body_refusal, _error, _ok
from tai42_channel_web.routes.session_access import (
    _CROSS_ORIGIN,
    _CROSS_ORIGIN_CODE,
    _SESSION_MISSING,
    _SESSION_MISSING_CODE,
    _serves,
    _session,
    _store_off,
)
from tai42_channel_web.session import is_cross_origin
from tai42_channel_web.settings import web_settings

# The multipart framing around the file part — the boundary lines, the part headers
# carrying the filename, the ``identity`` field — so a file of exactly the largest kind's
# cap still reaches the seam instead of tripping the body-limit backstop first.
_UPLOAD_MULTIPART_OVERHEAD_BYTES = 16 * 1024

# The route's declared request-body bound = the seam's largest per-kind cap plus that
# framing allowance, read once at import (re-read on every epoch build, whose settings
# reset precedes this module's re-import). ``BodyLimitMiddleware`` honours this bound
# for the matched door, and the door hands the SAME int to Starlette's multipart
# ``max_part_size`` so a field part is never rejected under it. The seam enforces the
# DERIVED kind's own cap on the file bytes mid-stream — the route bound only admits up
# to the worst case.
_UPLOAD_MAX_BODY_BYTES = media_ingest_cap_settings().max_cap + _UPLOAD_MULTIPART_OVERHEAD_BYTES

# Bytes read per ``UploadFile.read`` chunk streamed into the seam — the file is never
# read whole into memory.
_UPLOAD_READ_CHUNK_BYTES = 64 * 1024

# The upload door charges its own rate-limit family, disjoint from the message door's
# path-derived ``channels_web``: an upload carries up to the per-kind byte cap, a heavier
# cost class than a text send. ``RateLimitMiddleware`` honours this declared family over
# the path stem.
_UPLOAD_RATE_LIMIT_FAMILY = "channels_web_uploads"

_UPLOAD_DECLARED = DeclaredRouteMetadata(
    reload_gated=False,
    reads_body=True,
    error_statuses=(400, 401, 403, 413, 415, 422, 501, 503),
    success_status=200,
    max_body_bytes=_UPLOAD_MAX_BODY_BYTES,
    rate_limit_family=_UPLOAD_RATE_LIMIT_FAMILY,
)

# The rejection copy + machine code per ingest outcome (the page maps each code to its
# own inline copy). The door catches ONLY these seam errors; any other exception
# propagates.
_TOO_LARGE = "attachment is too large"
_TOO_LARGE_CODE = "media_too_large"
_TYPE_NOT_ALLOWED = "attachment type is not supported"
_TYPE_NOT_ALLOWED_CODE = "media_type_not_allowed"
_STORE_UNAVAILABLE = "attachment store is unavailable"
_STORE_UNAVAILABLE_CODE = "media_store_unavailable"
_READ_FAILED = "attachment upload could not be read"
_READ_FAILED_CODE = "media_read_failed"

# A part-parse failure, or a request carrying no file part at all.
_INVALID_UPLOAD = "invalid or missing upload"
_INVALID_UPLOAD_CODE = "invalid_upload"


async def _read_in_chunks(upload: UploadFile) -> AsyncIterator[bytes]:
    """Yield the uploaded file's bytes in bounded chunks, never reading it whole."""
    while True:
        chunk = await upload.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        yield chunk


def _single_upload(form: FormData) -> UploadFile | None:
    """The one uploaded file part in the form, or ``None`` when the request carries none.

    The form is parsed with ``max_files=1``, so at most one file part is present.
    """
    uploads = [value for _, value in form.multi_items() if isinstance(value, UploadFile)]
    return uploads[0] if uploads else None


async def _ingest_upload(upload: UploadFile, address: str) -> IngestedMedia | Response:
    """Stream the file through the seam as PENDING media, or a mapped typed refusal.

    The item is ingested with no owning message (``message_id=None``), owned by
    ``(channel_id="web", participant_identity=address)`` and held under the seam's
    pending TTL until the send binds it. Each seam error maps to its loud typed status:
    over-cap → 413, disallowed type → 415, no store → 503, unreadable source → 400.
    """
    origin = MediaOrigin(channel_id="web", participant_identity=address, message_id=None)
    try:
        return await tai42_app.media.ingest_media(
            source=_read_in_chunks(upload),
            kind_hint=None,
            declared_mime=upload.content_type,
            filename=upload.filename,
            declared_size=upload.size,
            origin=origin,
        )
    except MediaTooLargeError:
        return _error(_TOO_LARGE, 413, _TOO_LARGE_CODE)
    except MediaTypeNotAllowedError:
        return _error(_TYPE_NOT_ALLOWED, 415, _TYPE_NOT_ALLOWED_CODE)
    except MediaStoreUnavailableError:
        return _error(_STORE_UNAVAILABLE, 503, _STORE_UNAVAILABLE_CODE)
    except MediaSourceReadError:
        return _error(_READ_FAILED, 400, _READ_FAILED_CODE)


@tai42_app.http.custom_route(
    "/uploads",
    methods=["POST"],
    summary="Upload one file the visitor attaches to a web chat message",
    tags=["channels"],
    response_model=UploadAcceptedResponse,
    declared=_UPLOAD_DECLARED,
)
async def web_uploads(request: Request) -> Response:
    """Ingest one uploaded file as PENDING media and return its served-media reference.

    Same session model as the message door: a cross-origin POST is 403, an unconfigured
    store 501, a missing session 401, and a body ``identity`` the session was not minted
    on 401. The multipart body carries one file part and the ``identity`` field; a
    part-parse failure or a request with no file part is 400 ``invalid_upload``. The
    file streams through the ingest seam PENDING (``message_id=None``); on success the
    door returns ``{media_id, kind, mime, size, filename, url}`` via ``_ok``, and each
    ingest failure is its own loud typed ``{error, code}`` refusal.
    """
    if is_cross_origin(request):
        return _error(_CROSS_ORIGIN, 403, _CROSS_ORIGIN_CODE)
    off = _store_off()
    if off is not None:
        return off
    settings = web_settings()
    registration = await _session(request, settings)
    if registration is None:
        return _error(_SESSION_MISSING, 401, _SESSION_MISSING_CODE)
    try:
        form = await request.form(max_part_size=_UPLOAD_MAX_BODY_BYTES, max_files=1, max_fields=8)
    except (MultiPartException, HTTPException):
        return _error(_INVALID_UPLOAD, 400, _INVALID_UPLOAD_CODE)
    try:
        try:
            identity = IdentityBody.model_validate({"identity": form.get("identity")}).identity
        except ValidationError as exc:
            return _error(_body_refusal(exc), 422)
        if not _serves(registration, identity):
            return _error(_SESSION_MISSING, 401, _SESSION_MISSING_CODE)
        upload = _single_upload(form)
        if upload is None:
            return _error(_INVALID_UPLOAD, 400, _INVALID_UPLOAD_CODE)
        result = await _ingest_upload(upload, registration.visitor_id)
        if isinstance(result, Response):
            return result
        return _ok(
            {
                "media_id": result.media_id,
                "kind": result.item.kind.value,
                "mime": result.mime,
                "size": result.size,
                "filename": result.item.filename,
                "url": result.item.url,
            }
        )
    finally:
        await form.close()
