"""Always-bridge rich content: inbound media, location, contacts, and reactions.

None of these can answer a pending ask — each bridges as a fresh conversation turn.

INBOUND MEDIA DESIGN NOTE (design gap — see the plugin README and this lane's report):
WhatsApp inbound media arrives as a Graph media ``id`` (``GET {graph}/{id}`` returns a
SHORT-LIVED, Bearer-AUTHENTICATED lookaside url — not a durable public https url, and not a
valid :class:`MediaItem` source). Turning that id into a durable typed ``attachments`` entry
needs a served-media INGESTION seam (fetch the bytes, persist them, mint a
``{MEDIA_ROUTE_PREFIX}{id}`` served reference). The platform's served-media store
(``tai42_skeleton.interactions.media``) is NOT reachable from a channel plugin and handles
only outbound ``data:image`` substitution, so NO such seam exists for a channel today.
Rather than invent infrastructure or fabricate an unfetchable url, inbound media therefore
bridges as a turn WITHOUT a typed ``attachments`` entry: the caption becomes the turn text
(a faithful ``[kind]`` placeholder when caption-less) and the media's identity rides the
``media_*`` params (a consumer re-fetches via ``media_id`` with operator credentials).
The durable fix is a platform served-media ingestion seam on the app handle; until then this
is the honest minimal alternative. Inbound LOCATION, by contrast, has a fully in-contract
typed shape (:class:`LocationElement`) and DOES land on ``accept(location=...)``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from tai42_contract.interactions.models import LocationElement

from tai42_channel_whatsapp.correlation import already_seen
from tai42_channel_whatsapp.inbound.answers import _bridge_inbound
from tai42_channel_whatsapp.inbound.params import _merged_params, _put_param

logger = logging.getLogger(__name__)

# The faithful, ALWAYS non-blank placeholder text a caption-less media/location/contacts/
# reaction turn carries (``accept`` refuses blank text — the message must never be lost).
_MEDIA_PLACEHOLDERS = {
    "image": "[image]",
    "document": "[document]",
    "audio": "[audio]",
    "video": "[video]",
    "sticker": "[sticker]",
}


def _media_placeholder(message_type: str, media: dict[str, Any]) -> str:
    """A faithful, non-blank turn text for a caption-less media message.

    The bracketed type label, enriched for a document with a filename and a voice note. Never blank
    (``accept`` refuses blank text).
    """
    if message_type == "document":
        filename = media.get("filename")
        if isinstance(filename, str) and filename.strip():
            return f"[document: {filename.strip()}]"
    if message_type == "audio" and media.get("voice") is True:
        return "[voice message]"
    return _MEDIA_PLACEHOLDERS.get(message_type, "[media]")


async def _handle_media(
    message: dict[str, Any],
    phone_number_id: str,
    wa_id: str,
    wamid: str,
    params: dict[str, str],
    *,
    message_type: str,
) -> None:
    """Bridge an inbound media message (image/document/audio/video/sticker) as a fresh turn.

    The caption becomes the turn text (a faithful ``[kind]`` placeholder when caption-less),
    and the media's identity rides ``params`` (``media_kind``/``media_id``/``media_mime_type``/
    ``media_sha256``/``media_filename``/``media_voice``/``sticker_animated``). No typed
    ``attachments`` entry is minted — the Graph media id is not a durable :class:`MediaItem`
    source and no served-media ingestion seam is reachable from a channel; see the module's
    INBOUND MEDIA design note. Media never answers a pending ask (a photo cannot satisfy a
    text/select/form question): it always bridges, leaving any parked ask untouched.
    """
    if await already_seen(wamid):
        return
    media = message.get(message_type)
    media = media if isinstance(media, dict) else {}
    caption = media.get("caption")
    text = caption.strip() if isinstance(caption, str) and caption.strip() else _media_placeholder(message_type, media)

    media_params: dict[str, str] = {}
    _put_param(media_params, "media_kind", message_type)
    _put_param(media_params, "media_id", media.get("id"))
    _put_param(media_params, "media_mime_type", media.get("mime_type"))
    _put_param(media_params, "media_sha256", media.get("sha256"))
    _put_param(media_params, "media_filename", media.get("filename"))
    if media.get("voice") is True:
        _put_param(media_params, "media_voice", "true")
    if media.get("animated") is True:
        _put_param(media_params, "sticker_animated", "true")

    await _bridge_inbound(phone_number_id, wa_id, text, wamid, params=_merged_params(params, media_params))


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
