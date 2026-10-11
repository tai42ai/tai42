"""How an inbound media fetch-and-ingest fault is classified: transient (redeliver) or a participant-visible rejection.

Every channel that fetches a participant's media and ingests it into the served-media store catches
:data:`MEDIA_FETCH_AND_INGEST_FAULTS`, re-raises a transient one so its vendor redelivers, and answers
any other with :func:`media_rejection_reason`. Every exception outside the tuple propagates: a bare or
unexpected ingest failure surfaces loudly.
"""

from __future__ import annotations

from typing import Final

from tai42_contract.conversations import InboundMediaKind, InboundRejectionReason
from tai42_contract.interactions import (
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)

from tai42_kit.net import MediaFetchError, UrlGuardError

__all__ = [
    "MEDIA_FETCH_AND_INGEST_FAULTS",
    "inbound_media_kind_for_mime",
    "media_fault_is_transient",
    "media_rejection_reason",
]

#: The kit fetch faults and the contract ingest faults one inbound media fetch-and-ingest can raise.
MEDIA_FETCH_AND_INGEST_FAULTS: Final = (
    MediaFetchError,
    UrlGuardError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
    MediaStoreUnavailableError,
    MediaSourceReadError,
)


def media_fault_is_transient(exc: BaseException) -> bool:
    """Whether the fault is worth a redelivery: a transient fetch fault or a torn body read."""
    if isinstance(exc, MediaFetchError):
        return exc.transient
    return isinstance(exc, MediaSourceReadError)


def media_rejection_reason(exc: BaseException) -> InboundRejectionReason:
    """The participant-visible rejection reason: over-cap, a disallowed type, else could-not-receive."""
    if isinstance(exc, MediaTooLargeError):
        return InboundRejectionReason.TOO_LARGE
    if isinstance(exc, MediaTypeNotAllowedError):
        return InboundRejectionReason.UNSUPPORTED_TYPE
    return InboundRejectionReason.COULD_NOT_RECEIVE


_KIND_BY_MAJOR: Final = {
    "image": InboundMediaKind.IMAGE,
    "video": InboundMediaKind.VIDEO,
    "audio": InboundMediaKind.AUDIO,
    "application": InboundMediaKind.DOCUMENT,
    "text": InboundMediaKind.DOCUMENT,
}


def inbound_media_kind_for_mime(mime: str | None) -> InboundMediaKind:
    """The generic inbound-media kind for a vendor-declared mime type, by its major type.

    ``image``/``video``/``audio`` map to themselves, ``application``/``text`` to ``document``; any
    other major type and a missing or blank mime map to ``file``.
    """
    if not mime:
        return InboundMediaKind.FILE
    major = mime.split("/", 1)[0].strip().lower()
    return _KIND_BY_MAJOR.get(major, InboundMediaKind.FILE)
