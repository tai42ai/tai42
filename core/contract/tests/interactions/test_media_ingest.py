"""Tests for the served-media ingestion result/origin models and the error family."""

from __future__ import annotations

from tai42_contract.errors import ErrorKind
from tai42_contract.interactions import (
    IngestedMedia,
    MediaAlreadyBoundError,
    MediaIngestError,
    MediaItem,
    MediaKind,
    MediaNotFoundError,
    MediaOrigin,
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)

_MEDIA_ITEM = MediaItem(kind=MediaKind.IMAGE, url="data:image/png;base64,iVBORw0KGgo=")


def test_ingested_media_and_origin_construct():
    pending_origin = MediaOrigin(channel_id="c-1", participant_identity="p-1")
    assert pending_origin.message_id is None

    bound_origin = MediaOrigin(channel_id="c-1", participant_identity="p-1", message_id="m-1")
    assert bound_origin.message_id == "m-1"

    ingested = IngestedMedia(
        item=_MEDIA_ITEM,
        media_id="id-43",
        size=1024,
        sha256="deadbeef",
        mime="image/png",
    )
    assert ingested.pending is False
    assert ingested.item is _MEDIA_ITEM
    assert ingested.media_id == "id-43"
    assert ingested.size == 1024
    assert ingested.sha256 == "deadbeef"
    assert ingested.mime == "image/png"

    pending = IngestedMedia(item=_MEDIA_ITEM, media_id="id-43", size=1, sha256="x", mime="image/png", pending=True)
    assert pending.pending is True


def test_media_error_family_derives_from_base():
    for cls in (
        MediaTooLargeError,
        MediaTypeNotAllowedError,
        MediaStoreUnavailableError,
        MediaSourceReadError,
        MediaNotFoundError,
        MediaAlreadyBoundError,
    ):
        assert issubclass(cls, MediaIngestError)


def test_media_errors_importable_from_public_path():
    import tai42_contract.interactions as interactions

    names = [
        "MediaIngestError",
        "MediaTooLargeError",
        "MediaTypeNotAllowedError",
        "MediaStoreUnavailableError",
        "MediaSourceReadError",
        "MediaNotFoundError",
        "MediaAlreadyBoundError",
    ]
    for name in names:
        assert hasattr(interactions, name)
        assert name in interactions.__all__


def test_media_error_kinds():
    assert MediaIngestError("x").__tai_error_kind__ is ErrorKind.UPSTREAM_ERROR
    assert MediaTooLargeError("x").__tai_error_kind__ is ErrorKind.BAD_INPUT
    assert MediaTypeNotAllowedError("x").__tai_error_kind__ is ErrorKind.BAD_INPUT
    assert MediaStoreUnavailableError("x").__tai_error_kind__ is ErrorKind.UNAVAILABLE
    assert MediaSourceReadError("x").__tai_error_kind__ is ErrorKind.UPSTREAM_ERROR
    assert MediaNotFoundError("x").__tai_error_kind__ is ErrorKind.NOT_FOUND
    assert MediaAlreadyBoundError("x").__tai_error_kind__ is ErrorKind.CONFLICT
