"""The generic, cross-channel inbound-media wire vocabulary a bridged turn carries.

A channel that receives media a participant sent bridges it as a normal turn — the rendered
``text`` plus the opaque ``media_*`` entry-params this module shapes — never by fetching bytes and
never by minting typed ``attachments``. The platform attaches NO meaning and NO trust to the
values: a tool consumer opts into the keys it understands, the same way it reads any other
enrichment ``params``.

Every value is transport-bounded by :data:`~tai42_contract.entry_params.ENTRY_PARAM_VALUE_MAX_CHARS`;
an over-cap value is DROPPED, never truncated (truncating an opaque token silently corrupts it), and
the aggregate ``params`` bound (count / total bytes) is enforced separately at accept time by
:func:`~tai42_contract.conversations.validate_entry_params`, so this module applies only the
per-value cap.
"""

from __future__ import annotations

import logging
from enum import StrEnum

from tai42_contract.entry_params import ENTRY_PARAM_VALUE_MAX_CHARS

logger = logging.getLogger(__name__)


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


def _put_param(params: dict[str, str], key: str, value: str | None) -> None:
    """Add ``key`` iff ``value`` is a non-empty string within the per-value cap.

    An over-cap value is DROPPED (never truncated — truncation silently corrupts an opaque
    token); a debug line records the drop and NEVER logs the value.
    """
    if not value:
        return
    if len(value) > ENTRY_PARAM_VALUE_MAX_CHARS:
        logger.debug("dropping inbound media param %r: value over the %d-char cap", key, ENTRY_PARAM_VALUE_MAX_CHARS)
        return
    params[key] = value


def build_inbound_media_params(
    *,
    kind: InboundMediaKind | str,
    media_id: str | None = None,
    mime_type: str | None = None,
    filename: str | None = None,
    sha256: str | None = None,
    voice: bool = False,
    animated: bool = False,
    size: int | None = None,
) -> dict[str, str]:
    """The opaque ``media_*`` entry-params for one bridged inbound-media turn.

    Pure shaping, no I/O. ``kind`` is coerced through :class:`InboundMediaKind` so an
    unknown wire kind raises ``ValueError`` loudly (a channel that cannot map its vendor kind
    calls ``notify_inbound_rejected`` instead of bridging). Each value is bounded by the
    contract's per-value cap (over-cap dropped, never truncated); the aggregate ``params`` bound
    is enforced at accept time by :func:`~tai42_contract.conversations.validate_entry_params`.
    """
    resolved = InboundMediaKind(kind)
    params: dict[str, str] = {}
    _put_param(params, MEDIA_KIND_PARAM, resolved.value)
    _put_param(params, MEDIA_ID_PARAM, media_id)
    _put_param(params, MEDIA_MIME_TYPE_PARAM, mime_type)
    _put_param(params, MEDIA_SHA256_PARAM, sha256)
    _put_param(params, MEDIA_FILENAME_PARAM, filename)
    if voice:
        _put_param(params, MEDIA_VOICE_PARAM, "true")
    if animated:
        _put_param(params, STICKER_ANIMATED_PARAM, "true")
    if size is not None:
        _put_param(params, MEDIA_SIZE_PARAM, str(size))
    return params


def inbound_media_placeholder(kind: InboundMediaKind | str, *, filename: str | None = None, voice: bool = False) -> str:
    """The non-blank turn text for a caption-less inbound-media message.

    ``accept`` refuses blank text, so this guarantees the message is never lost. A voice audio
    reads ``[voice message]``; a document/file with a filename reads ``[document: <name>]`` /
    ``[file: <name>]``; otherwise the bracketed kind. Exhaustive over :class:`InboundMediaKind`
    (an unknown kind raises ``ValueError`` — never a silent ``[media]`` fallback).
    """
    resolved = InboundMediaKind(kind)
    if resolved is InboundMediaKind.AUDIO and voice:
        return "[voice message]"
    if resolved in (InboundMediaKind.DOCUMENT, InboundMediaKind.FILE):
        name = filename.strip() if isinstance(filename, str) and filename.strip() else None
        if name:
            return f"[{resolved.value}: {name}]"
    return f"[{resolved.value}]"


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
    "build_inbound_media_params",
    "inbound_media_placeholder",
]
