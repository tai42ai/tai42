"""Always-bridge rich content: inbound media, location, contacts, and reactions.

None of these can answer a pending ask — each bridges as a fresh conversation turn.

INBOUND MEDIA: WhatsApp media arrives as a Graph media ``id``. The channel looks up the object's
short-lived, Bearer-authenticated lookaside url (:func:`~tai42_channel_whatsapp.client.fetch_media_metadata`),
downloads the bytes in a single Bearer-authenticated request
(:func:`~tai42_channel_whatsapp.client.open_media_download`; redirects are not followed — a redirect
answer is a permanent fetch fault), and ingests them through the platform's ``app.media.ingest_media``
chokepoint, which caps, sniffs,
stores, and mints a served ``{MEDIA_ROUTE_PREFIX}{id}`` reference. The bridged turn then carries
BOTH the typed :class:`MediaItem` ``attachments`` entry and the parity ``media_*`` params off that
one ingest. A transient fetch fault (a 5xx, a timeout, a torn read) raises so the webhook returns
non-2xx and Meta redelivers (idempotent through ``already_seen``/``mark_seen``); a permanent fault
(the media is gone, over the cap, a disallowed type, or no store) rejects with a participant notice
and, when a caption rode with it, bridges the caption as a text-only turn so no message is lost.
Inbound LOCATION has a fully in-contract typed shape (:class:`LocationElement`) and lands on
``accept(location=...)``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.conversations import (
    InboundMediaKind,
    InboundRejectionReason,
    build_inbound_media_params,
    inbound_media_placeholder,
)
from tai42_contract.interactions import (
    IngestedMedia,
    MediaOrigin,
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)
from tai42_contract.interactions.models import LocationElement
from tai42_kit.net import MediaFetchError, UrlGuardError

from tai42_channel_whatsapp.client import fetch_media_metadata, media_endpoint_host, open_media_download
from tai42_channel_whatsapp.correlation import already_seen, mark_seen
from tai42_channel_whatsapp.inbound.answers import _bridge_inbound
from tai42_channel_whatsapp.inbound.params import _merged_params, _put_param

logger = logging.getLogger(__name__)


async def _handle_media(
    message: dict[str, Any],
    phone_number_id: str,
    wa_id: str,
    wamid: str,
    params: dict[str, str],
    *,
    message_type: str,
) -> None:
    """Fetch an inbound media message's bytes, ingest them, and bridge it as a fresh turn.

    Looks up the Graph media object's lookaside url, streams the bytes through
    ``app.media.ingest_media`` (which caps/sniffs/stores and mints a served reference), and
    bridges a turn carrying the typed :class:`MediaItem` ``attachments`` entry AND the parity
    ``media_*`` params off that one ingest. The caption is the turn text (a faithful ``[kind]``
    placeholder when caption-less); the placeholder label and ``media_filename`` both use the
    seam's SANITISED ``ingested.item.filename``, never the raw vendor name. A TRANSIENT fetch
    fault raises so Meta redelivers (the wamid is NOT marked seen); a PERMANENT fault rejects with
    a participant notice via :func:`_reject_media`. Media never answers a pending ask (a photo
    cannot satisfy a text/select/form question): it always bridges, leaving any parked ask
    untouched.
    """
    if await already_seen(wamid):
        return
    media = message.get(message_type)
    media = media if isinstance(media, dict) else {}
    kind = InboundMediaKind(message_type)
    caption = _media_caption(media)
    voice = media.get("voice") is True
    animated = media.get("animated") is True
    declared_mime = media.get("mime_type")

    try:
        ingested = await _fetch_and_ingest(media, kind, wa_id, wamid)
    except _MEDIA_FAILURES as exc:
        reason = _media_rejection_reason(exc)
        if reason is None:
            # Transient (a 5xx/timeout/torn read): 5xx the webhook so Meta redelivers — the wamid
            # is NOT marked seen, so the redelivery is re-processed rather than deduped away.
            raise
        await _reject_media(phone_number_id, wa_id, wamid, kind, caption, declared_mime, reason, params)
        return

    text = caption or inbound_media_placeholder(kind, filename=ingested.item.filename, voice=voice)
    media_params = build_inbound_media_params(
        kind=kind,
        media_id=ingested.media_id,
        mime_type=ingested.mime,
        sha256=ingested.sha256,
        filename=ingested.item.filename,
        voice=voice,
        animated=animated,
        size=ingested.size,
    )
    await _bridge_inbound(
        phone_number_id, wa_id, text, wamid, params=_merged_params(params, media_params), attachments=[ingested.item]
    )


# The kit fetch faults and the contract ingest faults one inbound media fetch can raise; every
# other exception propagates (a bare or unexpected ingest failure must surface loudly).
_MEDIA_FAILURES = (
    MediaFetchError,
    UrlGuardError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
    MediaStoreUnavailableError,
    MediaSourceReadError,
)


def _media_caption(media: dict[str, Any]) -> str | None:
    """The inbound media message's caption, stripped, or ``None`` when blank or absent."""
    caption = media.get("caption")
    if isinstance(caption, str) and caption.strip():
        return caption.strip()
    return None


def _media_rejection_reason(exc: Exception) -> InboundRejectionReason | None:
    """The rejection reason for a PERMANENT media failure, or ``None`` when the failure is TRANSIENT.

    ``None`` means the caller must re-raise (redeliver): a transport fault, a 5xx, a 408 or a 429 at
    fetch (``MediaFetchError.transient``) and a torn body read (``MediaSourceReadError``). Every other
    caught failure is permanent: over-cap → ``TOO_LARGE``, disallowed type → ``UNSUPPORTED_TYPE``,
    a gone media / SSRF-blocked url / absent store → ``COULD_NOT_RECEIVE``.
    """
    if isinstance(exc, MediaFetchError):
        return None if exc.transient else InboundRejectionReason.COULD_NOT_RECEIVE
    if isinstance(exc, MediaSourceReadError):
        return None
    if isinstance(exc, MediaTooLargeError):
        return InboundRejectionReason.TOO_LARGE
    if isinstance(exc, MediaTypeNotAllowedError):
        return InboundRejectionReason.UNSUPPORTED_TYPE
    return InboundRejectionReason.COULD_NOT_RECEIVE


async def _fetch_and_ingest(media: dict[str, Any], kind: InboundMediaKind, wa_id: str, wamid: str) -> IngestedMedia:
    """Look up the media object, stream its bytes, and ingest them into the served-media store.

    Raises the kit fetch faults / contract ingest faults for :func:`_handle_media` to classify; a
    media object with no fetchable id is a permanent, participant-visible failure (the metadata
    lookup already makes an absent lookaside ``url`` one). The vendor-declared mime/size ride
    the ingest, and the origin binds the item to this message at ingest.
    """
    media_id = media.get("id")
    if not isinstance(media_id, str) or not media_id:
        raise MediaFetchError(host=media_endpoint_host(), status_code=404)
    meta = await fetch_media_metadata(media_id)
    async with open_media_download(meta["url"]) as stream:
        return await tai42_app.media.ingest_media(
            source=stream.chunks,
            kind_hint=kind,
            declared_mime=meta.get("mime_type") or media.get("mime_type") or stream.content_type,
            filename=media.get("filename"),
            declared_size=meta.get("file_size") or stream.content_length,
            origin=MediaOrigin(channel_id="whatsapp", participant_identity=wa_id, message_id=wamid),
        )


async def _reject_media(
    phone_number_id: str,
    wa_id: str,
    wamid: str,
    kind: InboundMediaKind,
    caption: str | None,
    declared_mime: str | None,
    reason: InboundRejectionReason,
    base_params: dict[str, str],
) -> None:
    """Reject an inbound media the platform could not receive, notifying the participant once.

    Tells the participant once via the rejection facet. When a caption rode with the media its
    text is not lost — it bridges as a text-only turn carrying the generic kind + declared-mime
    params (no served id, sha, size, or filename: the media does not exist). With no caption the
    notice is the whole outcome. Either way the wamid is marked seen so a Meta redelivery is not
    re-processed.
    """
    await tai42_app.conversations.notify_inbound_rejected(
        channel_id="whatsapp",
        recipient=wa_id,
        sender_identity=phone_number_id,
        kind=kind.value,
        reason=reason,
    )
    if caption:
        media_params = build_inbound_media_params(kind=kind, mime_type=declared_mime)
        await _bridge_inbound(phone_number_id, wa_id, caption, wamid, params=_merged_params(base_params, media_params))
    else:
        await mark_seen(wamid)


async def _handle_location(
    message: dict[str, Any], phone_number_id: str, wa_id: str, wamid: str, params: dict[str, str]
) -> None:
    """Bridge an inbound location as a fresh turn carrying a typed :class:`LocationElement`.

    ``latitude``/``longitude`` build the typed ``location`` handed to ``accept`` (a
    machine-consumable field, not a param); the turn text is the shared ``name`` or
    ``address``, else the coordinates (never blank). A location whose coordinates are missing
    or out of the contract's ranges (or whose labels carry control characters) degrades to a
    text-only bridge — the reply is never lost. Location never answers a pending ask.
    """
    if await already_seen(wamid):
        return
    location_field = message.get("location")
    location_field = location_field if isinstance(location_field, dict) else {}
    latitude = location_field.get("latitude")
    longitude = location_field.get("longitude")
    name = location_field.get("name")
    address = location_field.get("address")
    name = name.strip() if isinstance(name, str) and name.strip() else None
    address = address.strip() if isinstance(address, str) and address.strip() else None

    location: LocationElement | None = None
    if isinstance(latitude, int | float) and isinstance(longitude, int | float):
        try:
            location = LocationElement(latitude=float(latitude), longitude=float(longitude), name=name, address=address)
        except ValueError as exc:
            logger.warning("whatsapp inbound location %s rejected (%s); bridging as text only", wamid, exc)

    if name:
        text = name
    elif address:
        text = address
    elif isinstance(latitude, int | float) and isinstance(longitude, int | float):
        text = f"location: {latitude}, {longitude}"
    else:
        text = "[location]"

    await _bridge_inbound(phone_number_id, wa_id, text, wamid, params=params or None, location=location)


async def _handle_contacts(
    message: dict[str, Any], phone_number_id: str, wa_id: str, wamid: str, params: dict[str, str]
) -> None:
    """Bridge inbound contact card(s) as a fresh turn.

    Contacts have no shared typed shape, so they ride ``params`` (per the contract's
    contacts→params guidance): ``contacts_count`` and the raw ``contacts`` array as compact
    JSON (dropped when it exceeds the per-value transport cap — ``contacts_count`` still
    rides). The turn text is the shared contact name(s), else a placeholder.
    """
    if await already_seen(wamid):
        return
    contacts = message.get("contacts")
    contacts = contacts if isinstance(contacts, list) else []
    names: list[str] = []
    for contact in contacts:
        name = contact.get("name") if isinstance(contact, dict) else None
        formatted = name.get("formatted_name") if isinstance(name, dict) else None
        if isinstance(formatted, str) and formatted.strip():
            names.append(formatted.strip())
    text = ", ".join(names) if names else "[contact card]"

    contact_params: dict[str, str] = {}
    _put_param(contact_params, "contacts_count", str(len(contacts)))
    _put_param(contact_params, "contacts", json.dumps(contacts, ensure_ascii=False))
    await _bridge_inbound(phone_number_id, wa_id, text, wamid, params=_merged_params(params, contact_params))


async def _handle_reaction(
    message: dict[str, Any], phone_number_id: str, wa_id: str, wamid: str, params: dict[str, str]
) -> None:
    """Bridge an inbound emoji reaction as a fresh turn.

    The reaction rides ``params`` (``reaction_emoji`` + ``reaction_message_id``, the ``wamid``
    it was applied to); an empty emoji is a REMOVED reaction (``reaction_emoji`` omitted). The
    turn text is the emoji, or a ``[reaction removed]`` placeholder. A reaction never answers a
    pending ask.
    """
    if await already_seen(wamid):
        return
    reaction = message.get("reaction")
    reaction = reaction if isinstance(reaction, dict) else {}
    emoji = reaction.get("emoji")
    text = emoji.strip() if isinstance(emoji, str) and emoji.strip() else "[reaction removed]"

    reaction_params: dict[str, str] = {}
    _put_param(reaction_params, "reaction_emoji", emoji)
    _put_param(reaction_params, "reaction_message_id", reaction.get("message_id"))
    await _bridge_inbound(phone_number_id, wa_id, text, wamid, params=_merged_params(params, reaction_params))
