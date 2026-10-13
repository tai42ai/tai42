"""The generic, cross-channel inbound-media wire vocabulary a bridged turn carries.

A channel that receives media a participant sent bridges it as a normal turn — the rendered
``text`` plus opaque ``media_*`` entry-params keyed by the names below — never by fetching bytes
and never by minting typed ``attachments``. The platform attaches NO meaning and NO trust to the
values: a tool consumer opts into the keys it understands, the same way it reads any other
enrichment ``params``. The turn text and params are shaped by
``tai42_kit.conversations.inbound_media``.
"""

from __future__ import annotations

from enum import StrEnum


class InboundMediaKind(StrEnum):
    """The generic inbound-media wire kind a channel resolves its vendor kind onto.

    The value rides the ``media_kind`` entry-param.
    """

    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    VIDEO = "video"
    STICKER = "sticker"
    FILE = "file"


class InboundRejectionReason(StrEnum):
    """Why an inbound content the channel recognises could not be bridged as a turn.

    Rides the ``reason`` of :meth:`~tai42_contract.app.facets.messaging.AppConversations.notify_inbound_rejected`;
    the participant-facing copy per reason lives in the skeleton impl (never in this vocabulary module).
    """

    UNSUPPORTED_TYPE = "unsupported_type"
    TOO_LARGE = "too_large"
    COULD_NOT_RECEIVE = "could_not_receive"


MEDIA_KIND_PARAM = "media_kind"
MEDIA_ID_PARAM = "media_id"
MEDIA_MIME_TYPE_PARAM = "media_mime_type"
MEDIA_FILENAME_PARAM = "media_filename"
MEDIA_SHA256_PARAM = "media_sha256"
MEDIA_VOICE_PARAM = "media_voice"
STICKER_ANIMATED_PARAM = "sticker_animated"
MEDIA_SIZE_PARAM = "media_size"


__all__ = [
    "MEDIA_FILENAME_PARAM",
    "MEDIA_ID_PARAM",
    "MEDIA_KIND_PARAM",
    "MEDIA_MIME_TYPE_PARAM",
    "MEDIA_SHA256_PARAM",
    "MEDIA_SIZE_PARAM",
    "MEDIA_VOICE_PARAM",
    "STICKER_ANIMATED_PARAM",
    "InboundMediaKind",
    "InboundRejectionReason",
]
