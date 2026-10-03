"""The upload door — PENDING ingest through the seam, the session model, and the loud
typed rejection mapping.

The seam is a local test double recording ``ingest_media`` (its origin, the streamed
source bytes, and the advisory metadata) and replaying a queued ``IngestedMedia`` or
raising a queued error, bound onto the shared stub app for each test."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from tai42_contract.interactions import (
    IngestedMedia,
    MediaKind,
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)
from tai42_kit.interactions import media_ingest_cap_settings

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web.routes.upload_routes import _UPLOAD_DECLARED, _UPLOAD_MULTIPART_OVERHEAD_BYTES

from .conftest import (
    _FOREIGN_ORIGIN,
    CLIENT_HOST,
    HOST,
    IDENTITY,
    OTHER_IDENTITY,
    SECURE_COOKIE,
    SESSION_TOKEN,
    VISITOR_ID,
    FakeRedis,
    _body,
    _handler,
    make_ingested_media,
)

pytestmark = pytest.mark.usefixtures("web_env")

_UPLOADS = "/uploads"
_UPLOADS_URL = "/api/channels/web/uploads"
_BOUNDARY = "test-upload-boundary"
# A small binary body standing in for a file part — no CRLF and no boundary run, so the
# bytes the door streams to the seam are reproduced exactly for the streaming assertion.
_IMAGE_BYTES = b"\x89PNG-image-\x00\x01\x02-payload"
_DEFAULT_FILE = ("file", "photo.png", "image/png", _IMAGE_BYTES)


class _IngestStub:
    """Records one ``ingest_media`` call and replays a queued result or raises a queued error.

    Drains the streamed ``source`` so a test can assert the door streamed the part (and its
    exact bytes) rather than reading it whole."""

    def __init__(self) -> None:
        self.ingest_calls: list[dict[str, Any]] = []
        self.streamed: bytes | None = None
        self.result: IngestedMedia | None = None
        self.error: Exception | None = None

    async def ingest_media(
        self,
        *,
        source: AsyncIterator[bytes],
        kind_hint: Any,
        declared_mime: str | None,
        filename: str | None,
        declared_size: int | None,
        origin: Any,
    ) -> IngestedMedia:
        collected = bytearray()
        async for chunk in source:
            collected.extend(chunk)
        self.streamed = bytes(collected)
        self.ingest_calls.append(
            {
                "kind_hint": kind_hint,
                "declared_mime": declared_mime,
                "filename": filename,
                "declared_size": declared_size,
                "origin": origin,
            }
        )
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("_IngestStub: no queued IngestedMedia for ingest_media")
        return self.result


@pytest.fixture
def ingest_stub(stub_app: Any) -> _IngestStub:
    """Bind an ingest-recording media double onto the stub app for the test."""
    stub = _IngestStub()
    stub_app.media = stub
    return stub


def _multipart(
    *,
    identity: str | None = IDENTITY,
    file: tuple[str, str, str, bytes] | None = _DEFAULT_FILE,
    extra_fields: list[tuple[str, str]] | None = None,
) -> bytes:
    """A ``multipart/form-data`` body with an optional ``identity`` field and file part."""
    lines: list[bytes] = []
    if identity is not None:
        lines += [
            f"--{_BOUNDARY}".encode(),
            b'Content-Disposition: form-data; name="identity"',
            b"",
            identity.encode(),
        ]
    for name, value in extra_fields or []:
        lines += [
            f"--{_BOUNDARY}".encode(),
            f'Content-Disposition: form-data; name="{name}"'.encode(),
            b"",
            value.encode(),
        ]
    if file is not None:
        name, filename, ctype, data = file
        lines += [
            f"--{_BOUNDARY}".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"'.encode(),
            f"Content-Type: {ctype}".encode(),
            b"",
            data,
        ]
    lines += [f"--{_BOUNDARY}--".encode(), b""]
    return b"\r\n".join(lines)


def _upload_request(
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    token: str | None = SESSION_TOKEN,
    cookie_name: str = SECURE_COOKIE,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
    **multipart_kwargs: Any,
):
    """A Starlette ``Request`` carrying a multipart upload, over a minimal ASGI scope.

    ``body``/``content_type`` override the whole multipart body/header (a malformed part);
    otherwise a well-formed body is built from ``multipart_kwargs`` under ``_BOUNDARY``."""
    from starlette.requests import Request

    payload = body if body is not None else _multipart(**multipart_kwargs)
    ctype = content_type if content_type is not None else f"multipart/form-data; boundary={_BOUNDARY}"
    headers = [(b"content-type", ctype.encode()), (b"host", HOST.encode("ascii"))]
    if token is not None:
        headers.append((b"cookie", f"{cookie_name}={token}".encode("ascii")))
    headers += extra_headers or []
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": _UPLOADS_URL,
        "raw_path": _UPLOADS_URL.encode("ascii"),
        "query_string": b"",
        "scheme": "https",
        "server": ("app-internal", 8000),
        "client": (CLIENT_HOST, 4711),
        "headers": headers,
        "path_params": {},
    }
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": payload, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(scope, receive)


# -- success ---------------------------------------------------------------------


async def test_upload_valid_image_returns_reference(web_env, stub_app, ingest_stub, registered_session: FakeRedis):
    ingest_stub.result = make_ingested_media(kind=MediaKind.IMAGE, mime="image/png", size=len(_IMAGE_BYTES))
    resp = await _handler(stub_app, _UPLOADS)(_upload_request())
    assert resp.status_code == 200
    data = _body(resp)["data"]
    assert data == {
        "media_id": "a" * 43,
        "kind": "image",
        "mime": "image/png",
        "size": len(_IMAGE_BYTES),
        "filename": None,
        "url": f"/api/interactions/media/{'a' * 43}",
    }


async def test_upload_ingests_pending_and_streams_the_part(
    web_env, stub_app, ingest_stub, registered_session: FakeRedis
):
    ingest_stub.result = make_ingested_media()
    await _handler(stub_app, _UPLOADS)(_upload_request())
    assert ingest_stub.streamed == _IMAGE_BYTES
    call = ingest_stub.ingest_calls[0]
    origin = call["origin"]
    # PENDING ingest owned by the visitor, no owning message yet.
    assert (origin.channel_id, origin.participant_identity, origin.message_id) == ("web", VISITOR_ID, None)
    # The browser content-type is advisory (the seam sniffs); the door passes it and the
    # actual part size, never a kind hint.
    assert call["declared_mime"] == "image/png"
    assert call["filename"] == "photo.png"
    assert call["declared_size"] == len(_IMAGE_BYTES)
    assert call["kind_hint"] is None


async def test_upload_document_returns_the_seams_sanitised_filename(
    web_env, stub_app, ingest_stub, registered_session: FakeRedis
):
    # The seam sanitises the browser filename; the door echoes ITS value, never the raw
    # upload name.
    ingest_stub.result = make_ingested_media(kind=MediaKind.DOCUMENT, mime="application/pdf", filename="report.pdf")
    resp = await _handler(stub_app, _UPLOADS)(
        _upload_request(file=("file", "../../etc/pa\nsswd.pdf", "application/pdf", b"%PDF-1.4 body"))
    )
    assert resp.status_code == 200
    data = _body(resp)["data"]
    assert data["filename"] == "report.pdf"
    assert data["kind"] == "document"
    assert ingest_stub.ingest_calls[0]["filename"] == "../../etc/pa\nsswd.pdf"


# -- session model ---------------------------------------------------------------


async def test_upload_cross_origin_is_403(web_env, stub_app, ingest_stub, registered_session: FakeRedis):
    resp = await _handler(stub_app, _UPLOADS)(_upload_request(extra_headers=_FOREIGN_ORIGIN))
    assert resp.status_code == 403
    assert _body(resp)["code"] == "origin_mismatch"
    assert ingest_stub.ingest_calls == []


async def test_upload_without_a_session_cookie_is_401(web_env, stub_app, ingest_stub, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _UPLOADS)(_upload_request(token=None))
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"
    assert ingest_stub.ingest_calls == []


async def test_upload_identity_the_session_was_not_minted_on_is_401(
    web_env, stub_app, ingest_stub, registered_session: FakeRedis
):
    resp = await _handler(stub_app, _UPLOADS)(_upload_request(identity=OTHER_IDENTITY))
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"
    assert ingest_stub.ingest_calls == []


async def test_upload_unconfigured_store_is_501(no_web_env, stub_app, ingest_stub):
    resp = await _handler(stub_app, _UPLOADS)(_upload_request())
    assert resp.status_code == 501
    assert _body(resp)["code"] == "web_transcript_store_off"
    assert ingest_stub.ingest_calls == []


async def test_upload_missing_identity_field_is_422(web_env, stub_app, ingest_stub, registered_session: FakeRedis):
    resp = await _handler(stub_app, _UPLOADS)(_upload_request(identity=None))
    assert resp.status_code == 422
    assert ingest_stub.ingest_calls == []


# -- malformed request -----------------------------------------------------------


async def test_upload_no_file_part_is_400_invalid_upload(web_env, stub_app, ingest_stub, registered_session: FakeRedis):
    resp = await _handler(stub_app, _UPLOADS)(_upload_request(file=None))
    assert resp.status_code == 400
    assert _body(resp)["code"] == "invalid_upload"
    assert ingest_stub.ingest_calls == []


async def test_upload_unparseable_multipart_is_400_invalid_upload(
    web_env, stub_app, ingest_stub, registered_session: FakeRedis
):
    # A multipart content-type with no boundary is unparseable — the door refuses it
    # loudly rather than 500ing.
    resp = await _handler(stub_app, _UPLOADS)(_upload_request(content_type="multipart/form-data", body=b"garbage"))
    assert resp.status_code == 400
    assert _body(resp)["code"] == "invalid_upload"
    assert ingest_stub.ingest_calls == []


async def test_a_second_file_part_is_refused_as_invalid_upload(
    web_env, stub_app, ingest_stub, registered_session: FakeRedis
):
    # The form is parsed with max_files=1; a second file part raises MultiPartException and
    # the door refuses it loudly rather than 500ing.
    body = b"\r\n".join(
        [
            f"--{_BOUNDARY}".encode(),
            b'Content-Disposition: form-data; name="identity"',
            b"",
            IDENTITY.encode(),
            f"--{_BOUNDARY}".encode(),
            b'Content-Disposition: form-data; name="file"; filename="a.png"',
            b"Content-Type: image/png",
            b"",
            _IMAGE_BYTES,
            f"--{_BOUNDARY}".encode(),
            b'Content-Disposition: form-data; name="file2"; filename="b.png"',
            b"Content-Type: image/png",
            b"",
            _IMAGE_BYTES,
            f"--{_BOUNDARY}--".encode(),
            b"",
        ]
    )
    resp = await _handler(stub_app, _UPLOADS)(_upload_request(body=body))
    assert resp.status_code == 400
    assert _body(resp)["code"] == "invalid_upload"
    assert ingest_stub.ingest_calls == []


async def test_more_than_eight_fields_is_refused_as_invalid_upload(
    web_env, stub_app, ingest_stub, registered_session: FakeRedis
):
    # The form is parsed with max_fields=8; the identity field plus nine extra fields
    # exceed it, raising MultiPartException — the door refuses it loudly rather than 500ing.
    resp = await _handler(stub_app, _UPLOADS)(_upload_request(extra_fields=[(f"x{i}", "v") for i in range(9)]))
    assert resp.status_code == 400
    assert _body(resp)["code"] == "invalid_upload"
    assert ingest_stub.ingest_calls == []


# -- the seam's typed rejection mapping ------------------------------------------


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (MediaTooLargeError("over the image cap"), 413, "media_too_large"),
        (MediaTypeNotAllowedError("svg is active content"), 415, "media_type_not_allowed"),
        (MediaStoreUnavailableError("no blob provider"), 503, "media_store_unavailable"),
        (MediaSourceReadError("torn stream"), 400, "media_read_failed"),
    ],
)
async def test_upload_seam_error_maps_to_its_status_and_code(
    web_env, stub_app, ingest_stub, registered_session: FakeRedis, error: Exception, status: int, code: str
):
    ingest_stub.error = error
    resp = await _handler(stub_app, _UPLOADS)(_upload_request())
    assert resp.status_code == status
    assert _body(resp)["code"] == code


async def test_image_over_image_cap_rejected_by_seam(web_env, stub_app, ingest_stub, registered_session: FakeRedis):
    # A part UNDER the route's largest bound but OVER the sniffed image kind's cap: the
    # door streams it to the seam, which reads the running total and raises — 413.
    ingest_stub.error = MediaTooLargeError("image exceeds its per-kind cap")
    resp = await _handler(stub_app, _UPLOADS)(_upload_request())
    assert resp.status_code == 413
    assert _body(resp)["code"] == "media_too_large"
    # The door reached the seam and streamed the part (the cap is enforced mid-stream,
    # not by the route bound).
    assert ingest_stub.streamed == _IMAGE_BYTES


# -- the declared classification + body bound (honoured by the skeleton middlewares) --


def test_upload_route_classifies_into_uploads_family():
    # The door declares its own rate-limit family, disjoint from the message door's
    # path-derived ``channels_web``. RateLimitMiddleware honours a declared family over
    # the path stem (skeleton-tested); the door's responsibility is the declaration.
    assert _UPLOAD_DECLARED.rate_limit_family == "channels_web_uploads"
    assert _UPLOAD_DECLARED.rate_limit_family != "channels_web"


def test_upload_route_declares_body_bound(no_web_env):
    # The declared per-route body bound is the seam's largest per-kind cap (its worst-case
    # pre-sniff ceiling) plus the multipart framing around a cap-sized file, so such a file
    # reaches the seam rather than the backstop. BodyLimitMiddleware honours this bound for
    # the matched door (skeleton-tested); the door's responsibility is the declaration.
    assert _UPLOAD_DECLARED.max_body_bytes == media_ingest_cap_settings().max_cap + _UPLOAD_MULTIPART_OVERHEAD_BYTES
    assert 0 < _UPLOAD_MULTIPART_OVERHEAD_BYTES < media_ingest_cap_settings().max_cap
    assert _UPLOAD_DECLARED.reads_body is True
    assert _UPLOAD_DECLARED.reload_gated is False
    assert _UPLOAD_DECLARED.success_status == 200
    assert _UPLOAD_DECLARED.error_statuses == (400, 401, 403, 413, 415, 422, 501, 503)
