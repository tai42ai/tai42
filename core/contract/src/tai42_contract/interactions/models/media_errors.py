"""Errors the served-media seam (ingest, bind, read) raises.

One family so a consumer catches :class:`MediaIngestError` for every failure the seam itself raises.
Messages are CONSTANT-SAFE — they carry sizes/kinds/limits, never the ingested bytes or a credential.
"""

from __future__ import annotations

from tai42_contract.errors import ErrorKind


class MediaIngestError(Exception):
    """Base for every served-media seam failure (ingest, bind, read)."""

    # A bare ingest failure is an upstream (fetch/store-side) fault.
    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR


class MediaTooLargeError(MediaIngestError):
    """The media exceeds the per-kind byte cap, or on a read the caller's ``max_bytes``.

    Raised BEFORE the write — from the declared size, or the instant the streamed running total
    crosses the cap; on a read, from the stored size before the bytes are handed back. The body is
    NEVER truncated.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT


class MediaTypeNotAllowedError(MediaIngestError):
    """The sniffed type is not on its kind's allowlist, or declared and sniffed MIME disagree.

    Active content (svg/html/script) is never on any allowlist.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT


class MediaStoreUnavailableError(MediaIngestError):
    """No blob provider is registered, or the provider write raised a store-boundary error.

    The honest loud absence — an inbound media is NEVER silently dropped when no store backs it.
    On a read: the media's record exists and no blob provider is registered to load its bytes.
    """

    __tai_error_kind__ = ErrorKind.UNAVAILABLE


class MediaSourceReadError(MediaIngestError):
    """The byte ``source`` raised while reading — a vendor fetch fault or a torn upload stream."""

    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR


class MediaNotFoundError(MediaIngestError):
    """No media matches the ``media_id`` for a bind or a read.

    For :meth:`~tai42_contract.app.facets.media.AppMedia.bind_media`: the ONE uniform error for an
    unknown id, an expired-pending id, OR an id not owned by the caller's
    ``(channel_id, participant_identity)`` — the three are indistinguishable to the caller, so there
    is no ownership oracle. For :meth:`~tai42_contract.app.facets.media.AppMedia.read_media`: the
    one uniform error for a malformed, unknown or still-pending id, an id whose media is removed,
    an inbound record whose bytes are absent, and a deployment with no media store configured.
    """

    __tai_error_kind__ = ErrorKind.NOT_FOUND


class MediaAlreadyBoundError(MediaIngestError):
    """The ``media_id`` is already bound to a DIFFERENT message than the one binding it now.

    A re-bind to the SAME message is idempotent (no error); a bind to a different message conflicts.
    """

    __tai_error_kind__ = ErrorKind.CONFLICT


__all__ = [
    "MediaAlreadyBoundError",
    "MediaIngestError",
    "MediaNotFoundError",
    "MediaSourceReadError",
    "MediaStoreUnavailableError",
    "MediaTooLargeError",
    "MediaTypeNotAllowedError",
]
