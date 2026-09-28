"""Fetching, ingesting, and bridging an inbound Telegram media member as typed served media.

A message carrying a photo/document/audio/voice/video/video_note/animation/sticker is mapped
onto the generic inbound-media vocabulary, its bytes fetched through ``getFile`` + the file host
and ingested through the platform's ONE ``ingest_media`` chokepoint, and the resulting turn routed
by the same reply-correlation the text path uses. A transient fetch/ingest failure raises so
Telegram redelivers; a permanent one notifies the participant and acks, preserving any caption.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.conversations import (
    InboundMediaKind,
    InboundRejectionReason,
    build_inbound_media_params,
    inbound_media_placeholder,
)
from tai42_contract.interactions import (
    MediaOrigin,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)
from tai42_kit.net import MediaFetchError, UrlGuardError, open_media_stream
from tai42_kit.settings import require_secret

from tai42_channel_telegram.client import TelegramFilePermanentError, get_file
from tai42_channel_telegram.inbound_bridge import _bridge, _ignored, _our_identity, _resolve_or_bridge
from tai42_channel_telegram.settings import TelegramSettings

logger = logging.getLogger(__name__)


class _MediaMember(NamedTuple):
    """One resolved Telegram media member, mapped onto the generic inbound-media vocabulary.

    ``file_id`` is fetched through ``getFile`` + the file host; ``declared_mime`` /
    ``filename`` / ``declared_size`` are the vendor-declared metadata (absent on the members
    that carry none), handed to the ingestion seam as hints only.
    """

    file_id: str
    kind: InboundMediaKind
    declared_mime: str | None
    filename: str | None
    declared_size: int | None
    voice: bool
    animated: bool


class _MediaMessage(NamedTuple):
    """A message carrying one media member to fetch, ingest, and bridge as a typed attachment."""

    chat: dict[str, object]
    chat_id: int
    member: _MediaMember
    caption: str | None


class _MediaSpec(NamedTuple):
    """How one Telegram media member maps onto the generic inbound-media vocabulary."""

    member: str
    kind: InboundMediaKind
    has_mime: bool
    has_filename: bool
    voice: bool
    animated_field: str | None


# Dict-valued media members, in resolution order: ``animation`` before ``document`` because
# Telegram sends an animation as BOTH members and the animation mapping is the correct one.
_MEDIA_SPECS = (
    _MediaSpec("animation", InboundMediaKind.VIDEO, has_mime=True, has_filename=True, voice=False, animated_field=None),
    _MediaSpec(
        "video_note", InboundMediaKind.VIDEO, has_mime=False, has_filename=False, voice=False, animated_field=None
    ),
    _MediaSpec("video", InboundMediaKind.VIDEO, has_mime=True, has_filename=True, voice=False, animated_field=None),
    _MediaSpec("audio", InboundMediaKind.AUDIO, has_mime=True, has_filename=True, voice=False, animated_field=None),
    _MediaSpec("voice", InboundMediaKind.AUDIO, has_mime=True, has_filename=False, voice=True, animated_field=None),
    _MediaSpec(
        "sticker",
        InboundMediaKind.STICKER,
        has_mime=False,
        has_filename=False,
        voice=False,
        animated_field="is_animated",
    ),
    _MediaSpec(
        "document", InboundMediaKind.DOCUMENT, has_mime=True, has_filename=True, voice=False, animated_field=None
    ),
)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _resolve_media(message: dict[str, object]) -> _MediaMember | None:
    """The resolved :class:`_MediaMember` for a media message, or ``None`` when it carries none.

    A photo resolves to its largest size (Telegram sends sizes in ascending order, so the last
    entry). A member with no ``file_id`` cannot be fetched and is skipped as if absent.
    """
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        largest = photo[-1]
        if isinstance(largest, dict):
            file_id = _str_or_none(largest.get("file_id"))
            if file_id is not None:
                return _MediaMember(
                    file_id=file_id,
                    kind=InboundMediaKind.IMAGE,
                    declared_mime=None,
                    filename=None,
                    declared_size=_int_or_none(largest.get("file_size")),
                    voice=False,
                    animated=False,
                )
    for spec in _MEDIA_SPECS:
        member = message.get(spec.member)
        if not isinstance(member, dict):
            continue
        file_id = _str_or_none(member.get("file_id"))
        if file_id is None:
            continue
        return _MediaMember(
            file_id=file_id,
            kind=spec.kind,
            declared_mime=_str_or_none(member.get("mime_type")) if spec.has_mime else None,
            filename=_str_or_none(member.get("file_name")) if spec.has_filename else None,
            declared_size=_int_or_none(member.get("file_size")),
            voice=spec.voice,
            animated=bool(member.get(spec.animated_field)) if spec.animated_field else False,
        )
    return None


async def _bridge_media(
    settings: TelegramSettings, media: _MediaMessage, message: dict[str, object], update: dict[str, object]
) -> Response:
    """Fetch one Telegram media member, ingest it into served media, then resolve or bridge the turn.

    ``getFile`` resolves the ``file_id`` to a file path; the bytes stream from
    ``{api_base_url}/file/bot{token}/{file_path}`` (the token rides the URL PATH — the kit's
    URL-free faults keep it out of every error/log) through the ONE ``ingest_media`` chokepoint,
    which caps, sniffs, stores, and mints a served reference. The ingest happens FIRST; the turn is
    then routed by the same reply-correlation the text path uses (:func:`_resolve_or_bridge`): a
    reply to a still-pending ask from a recipient chat resolves that ask with the caption/placeholder
    text and the parity ``media_*`` params (the answer ladder carries no typed attachment, so the
    attachment rides only the bridge fallthrough), otherwise the typed :class:`MediaItem` turn bridges.

    A TRANSIENT failure (a ``getFile`` transport fault / 5xx / 408 / 429, a stream-open
    connect/timeout/5xx/408/429, or a torn body read) RAISES so Telegram redelivers (``update_id``
    dedupes it). A PERMANENT failure (``getFile`` for an unknown/expired file, an over-cap or
    disallowed body, a store-unavailable outcome, any other 4xx or a redirect at open, an SSRF
    rejection) notifies the participant
    and acks; a caption present then ALSO bridges a text-only turn so the message is never lost. A
    rejected media that was a reply to a pending ask never resolves it — the ask stays pending and
    the participant is told the media could not be received.
    """
    our_identity = _our_identity(settings)
    if isinstance(our_identity, JSONResponse):
        return our_identity
    token = require_secret(settings.bot_token, "the telegram channel", "CHANNEL_TELEGRAM_BOT_TOKEN")
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return JSONResponse({"error": "update carries no integer update_id"}, status_code=400)

    member = media.member
    try:
        file_path = await get_file(token, member.file_id)
    except TelegramFilePermanentError as exc:
        logger.warning("telegram inbound: getFile permanently failed for chat_id=%s: %s", media.chat_id, exc)
        return await _reject_media(settings, media, update, InboundRejectionReason.COULD_NOT_RECEIVE)

    file_url = f"{settings.api_base_url}/file/bot{token}/{file_path}"
    origin = MediaOrigin(channel_id="telegram", participant_identity=str(media.chat_id), message_id=str(update_id))
    try:
        async with open_media_stream(file_url) as stream:
            ingested = await tai42_app.media.ingest_media(
                source=stream.chunks,
                kind_hint=member.kind,
                declared_mime=member.declared_mime or stream.content_type,
                filename=member.filename,
                declared_size=member.declared_size or stream.content_length,
                integrity_sha256=None,
                origin=origin,
            )
    except MediaFetchError as exc:
        if exc.transient:
            raise
        return await _reject_media(settings, media, update, InboundRejectionReason.COULD_NOT_RECEIVE)
    except UrlGuardError:
        return await _reject_media(settings, media, update, InboundRejectionReason.COULD_NOT_RECEIVE)
    except MediaTooLargeError:
        return await _reject_media(settings, media, update, InboundRejectionReason.TOO_LARGE)
    except MediaTypeNotAllowedError:
        return await _reject_media(settings, media, update, InboundRejectionReason.UNSUPPORTED_TYPE)
    except MediaStoreUnavailableError:
        return await _reject_media(settings, media, update, InboundRejectionReason.COULD_NOT_RECEIVE)

    text = media.caption or inbound_media_placeholder(member.kind, filename=ingested.item.filename, voice=member.voice)
    params = build_inbound_media_params(
        kind=member.kind,
        media_id=ingested.media_id,
        mime_type=ingested.mime,
        sha256=ingested.sha256,
        filename=ingested.item.filename,
        voice=member.voice,
        animated=member.animated,
        size=ingested.size,
    )
    return await _resolve_or_bridge(
        settings, media.chat, media.chat_id, text, message, update, params=params, attachments=[ingested.item]
    )


async def _reject_media(
    settings: TelegramSettings, media: _MediaMessage, update: dict[str, object], reason: InboundRejectionReason
) -> Response:
    """Notify the participant a media could not be bridged, preserve any caption, and ack.

    Routes through the shared ``notify_inbound_rejected`` chokepoint (the single participant
    notice + operator event). When the message carried a caption, ALSO bridges a text-only turn
    carrying it — the served media does not exist, so no ``media_id``/sha/size/filename and never
    the raw vendor filename, only the kind and the vendor-declared mime. No caption -> notice + ack.
    """
    our_identity = _our_identity(settings)
    if isinstance(our_identity, JSONResponse):
        return our_identity
    member = media.member
    await tai42_app.conversations.notify_inbound_rejected(
        channel_id="telegram",
        recipient=str(media.chat_id),
        sender_identity=our_identity,
        kind=member.kind.value,
        reason=reason,
    )
    if media.caption is not None:
        # A rejected media that was a reply to a pending ask never resolves it (the media does not
        # exist): the caption bridges as a fresh turn and the ask stays pending.
        text_params = build_inbound_media_params(kind=member.kind, mime_type=member.declared_mime)
        return await _bridge(settings, media.chat_id, media.caption, update, params=text_params)
    return _ignored("media rejected")
