"""The inbound media fault classifier: transient or rejected, the rejection reason, and the mime-to-kind map."""

from __future__ import annotations

import pytest
from tai42_contract.conversations import InboundMediaKind, InboundRejectionReason
from tai42_contract.interactions import (
    MediaAlreadyBoundError,
    MediaIngestError,
    MediaNotFoundError,
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)

from tai42_kit.net import MediaFetchError, UrlGuardError
from tai42_kit.net.media_faults import (
    MEDIA_FETCH_AND_INGEST_FAULTS,
    inbound_media_kind_for_mime,
    media_fault_is_transient,
    media_rejection_reason,
)


@pytest.mark.parametrize(
    ("exc", "transient", "reason"),
    [
        (MediaFetchError(host="cdn.example", status_code=503), True, InboundRejectionReason.COULD_NOT_RECEIVE),
        (MediaFetchError(host="cdn.example", status_code=429), True, InboundRejectionReason.COULD_NOT_RECEIVE),
        (
            MediaFetchError(host="cdn.example", cause_class="ReadTimeout"),
            True,
            InboundRejectionReason.COULD_NOT_RECEIVE,
        ),
        (MediaFetchError(host="cdn.example", status_code=404), False, InboundRejectionReason.COULD_NOT_RECEIVE),
        (UrlGuardError("blocked"), False, InboundRejectionReason.COULD_NOT_RECEIVE),
        (MediaTooLargeError("over"), False, InboundRejectionReason.TOO_LARGE),
        (MediaTypeNotAllowedError("type"), False, InboundRejectionReason.UNSUPPORTED_TYPE),
        (MediaStoreUnavailableError("store"), False, InboundRejectionReason.COULD_NOT_RECEIVE),
        (MediaSourceReadError("torn"), True, InboundRejectionReason.COULD_NOT_RECEIVE),
    ],
)
def test_each_fault_classifies(exc: Exception, transient: bool, reason: InboundRejectionReason) -> None:
    assert isinstance(exc, MEDIA_FETCH_AND_INGEST_FAULTS)
    assert media_fault_is_transient(exc) is transient
    assert media_rejection_reason(exc) is reason


@pytest.mark.parametrize(
    "exc",
    [MediaIngestError("bare"), MediaNotFoundError("gone"), MediaAlreadyBoundError("bound"), RuntimeError("bug")],
)
def test_a_fault_outside_the_tuple_is_not_caught_by_a_channel(exc: Exception) -> None:
    # A channel catches exactly the tuple, so every other fault propagates loudly.
    assert not isinstance(exc, MEDIA_FETCH_AND_INGEST_FAULTS)


class _UnrelatedIngestError(MediaIngestError):
    """An ingest fault subclass the classifier does not know."""


def test_an_unrelated_ingest_subclass_is_not_in_the_tuple() -> None:
    assert not isinstance(_UnrelatedIngestError("x"), MEDIA_FETCH_AND_INGEST_FAULTS)


@pytest.mark.parametrize(
    ("mime", "kind"),
    [
        ("image/png", InboundMediaKind.IMAGE),
        ("video/mp4", InboundMediaKind.VIDEO),
        ("audio/mpeg", InboundMediaKind.AUDIO),
        ("application/pdf", InboundMediaKind.DOCUMENT),
        ("text/plain", InboundMediaKind.DOCUMENT),
        ("Image/JPEG", InboundMediaKind.IMAGE),
        ("model/gltf-binary", InboundMediaKind.FILE),
        ("model/x", InboundMediaKind.FILE),
        (None, InboundMediaKind.FILE),
        ("", InboundMediaKind.FILE),
    ],
)
def test_the_mime_major_type_picks_the_kind(mime: str | None, kind: InboundMediaKind) -> None:
    assert inbound_media_kind_for_mime(mime) is kind
