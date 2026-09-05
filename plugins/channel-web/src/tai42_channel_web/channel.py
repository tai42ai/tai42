"""The web chat channel: ``deliver`` posts one question into a visitor's chat
transcript, ``notify`` posts one fire-and-forget agent message.

Both ends of a web conversation are the SAME anonymous visitor: they send inbound
messages through the public message door and receive agent output on their own SSE
stream. The transcript is keyed by ``(identity, address)`` — the web route identity
and that visitor's id.

``ChannelDelivery`` carries no ``sender_identity``, so a web ask names its target as
``recipient = "<identity>:<visitor-id>"``: the channel splits it back into
the transcript pair and stores both in the pending-question record so the later
``chat.answered`` frame lands on the same stream. ``notify`` takes either shape:
the conversation bridge sets ``sender_identity`` and a bare visitor-id
``recipient``; the ``notify_user`` door sets no ``sender_identity``, so its
``recipient`` carries the same composite encoding as a delivery.

This channel advertises media, interactive (options) and form notification support
but no template capability: a template is a vendor construct, so the central
``notify_user`` guard refuses a template send before it reaches here and the
defensive guard below refuses one loudly if it ever arrives. A media or options
notification lands as one durable ``chat.media`` transcript entry the page renders
as a card; a schema notification (an ask-less form) lands as one ``chat.form``
entry the page renders as a fillable card, submittable through the plugin's own
form door by the token the card carries.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime
from typing import ClassVar

from tai42_contract.channels import (
    ChannelDelivery,
    ChannelDeliveryError,
    ChannelNotification,
    LinkOption,
    Option,
    OptionSection,
    ReplyOption,
)
from tai42_contract.interactions.models import LocationElement, MediaItem

from tai42_channel_web.store import (
    FormRecord,
    QuestionRecord,
    append_form,
    append_media,
    append_message,
    append_question,
    release_question,
    reserve_question,
    store_form_record,
    transcript_order,
)

logger = logging.getLogger(__name__)

_NO_RECIPIENT = "no recipient requested and this channel has no default recipient; name the visitor"

# The doors refuse an identity outside this shape, so a send that carried one would
# write a transcript key no visitor's stream can ever read.
_MAX_IDENTITY_CHARS = 256

# Releases still in flight after their ``deliver`` was cancelled. A task with no
# strong reference can be garbage-collected mid-flight, which would leave exactly the
# pinned reservation the shield exists to prevent.
_pending_releases: set[asyncio.Task[None]] = set()

# The only answer format whose widget needs the interaction's callback ticket in the
# transcript frame — the visitor opens that link themselves. Every other format is
# answered by interaction id through this plugin's own door, so the ticket stays
# server-side.
_EXTERNAL_FORMAT = "external"


def _media_frame_item(item: MediaItem) -> dict[str, str]:
    """One media item as its transcript-frame shape — ``caption`` and ``filename``
    omitted when absent, so there is no empty-value key. ``filename`` rides a
    ``document`` only (the contract refuses it on any other kind), so a reader keys
    the download label off the document card alone."""
    entry = {"kind": item.kind.value, "url": item.url}
    if item.caption is not None:
        entry["caption"] = item.caption
    if item.filename is not None:
        entry["filename"] = item.filename
    return entry


def _option_frame_item(option: Option) -> dict[str, str]:
    """One tappable option as its transcript-frame shape, discriminated on ``kind``.

    A :class:`ReplyOption` becomes ``{"kind": "reply", "text", "description"?, "id"?}``
    — a tap submits ``text`` as the visitor's next message and, when the author set an
    ``id``, that id rides the submission as opaque enrichment (``params.reply_id``, the
    same convention every channel keeps). A :class:`LinkOption` becomes
    ``{"kind": "link", "label", "url"}`` — a tap OPENS ``url`` and submits nothing.
    Optional keys are omitted when absent, so there is no empty-value shape."""
    if isinstance(option, LinkOption):
        return {"kind": "link", "label": option.label, "url": option.url}
    entry = {"kind": "reply", "text": option.text}
    if option.description is not None:
        entry["description"] = option.description
    if option.id is not None:
        entry["id"] = option.id
    return entry


def _reply_frame_item(reply: ReplyOption) -> dict[str, str]:
    """One sectioned-list row as its frame shape — a :class:`ReplyOption` is the only
    row a section holds (a link is a button, never a list row), so this narrows
    :func:`_option_frame_item` to the reply case."""
    return _option_frame_item(reply)


def _section_frame_item(section: OptionSection) -> dict[str, object]:
    """One titled section as its frame shape: ``{"title", "rows": [reply, ...]}`` — the
    grouped reply rows the page renders under a section header."""
    return {"title": section.title, "rows": [_reply_frame_item(row) for row in section.rows]}


def _location_frame_item(location: LocationElement) -> dict[str, object]:
    """A shared geographic point as its frame shape: the coordinates plus any name and
    address, each omitted when absent. The page renders it as a map-pin element with an
    OpenStreetMap link built from the coordinates — no external tiles, CSP-safe."""
    entry: dict[str, object] = {"latitude": location.latitude, "longitude": location.longitude}
    if location.name is not None:
        entry["name"] = location.name
    if location.address is not None:
        entry["address"] = location.address
    return entry


def _require_recipient(requested: str | None, message: str) -> str:
    """The caller-supplied address, or raise ``ChannelDeliveryError`` — this channel
    has no operator default recipient."""
    if requested is None:
        raise ChannelDeliveryError(message)
    return requested


def _canonical_identity(value: str) -> str:
    """The identity trimmed to the bridge's canonical form, so a transcript key here
    matches the one the doors write.

    The same shape the doors enforce: blank, over-long, or carrying the ``:`` that
    separates a composite recipient is refused LOUDLY. Writing one anyway would key a
    transcript the stream door can never be asked for, so the ask would black-hole
    silently instead of failing."""
    identity = value.strip()
    if not identity or len(identity) > _MAX_IDENTITY_CHARS or ":" in identity:
        raise ChannelDeliveryError(
            f"web send names {value!r}, which is not a usable web route identity "
            f"(non-blank, ':'-free, at most {_MAX_IDENTITY_CHARS} characters); it qualifies the transcript key"
        )
    return identity


async def _release_shielded(interaction_id: str) -> None:
    """Free a reservation even while the caller is being cancelled.

    ``CancelledError`` is a ``BaseException``, so it slips past ``except Exception``,
    and a plain ``await`` in the unwind is cancelled again before it reaches Redis.
    The release runs as its own shielded task instead — the reservation is freed
    rather than pinning the interaction until its TTL."""
    task = asyncio.ensure_future(release_question(interaction_id))
    _pending_releases.add(task)
    task.add_done_callback(_pending_releases.discard)
    await asyncio.shield(task)


def _split_recipient(recipient: str | None) -> tuple[str, str]:
    """A sender-identity-less web send names its transcript pair as
    ``"<identity>:<visitor-id>"``; split it into ``(identity, address)`` or refuse
    loudly. The split takes the LAST colon: a visitor id is minted from the urlsafe
    alphabet and so is guaranteed ``:``-free. Whatever is left of it must be a usable
    identity, which ``_canonical_identity`` then enforces — a composite carrying a
    second colon is refused, not silently split into an unreadable transcript key."""
    value = _require_recipient(recipient, _NO_RECIPIENT)
    identity, separator, address = value.rpartition(":")
    if not separator or not identity or not address:
        raise ChannelDeliveryError(f"web recipient must be '<identity>:<visitor-id>', got {value!r}")
    return _canonical_identity(identity), address


class WebChannel:
    """Satisfies the ``tai42_contract.channels.Channel`` protocol.

    Advertises ``supports_form_delivery`` — the chat page renders a schema-driven
    form widget, so a ``form`` question is delivered here. ``notify`` renders the WHOLE
    interactive vocabulary the page owns pixel-for-pixel: media cards (image inline,
    document as a download card, video/audio as native players, link as a safe anchor),
    tappable reply chips and link-action buttons, sectioned reply lists, a media header
    and a muted footer, a shared location as a map-pin element, and ask-less forms —
    ``supports_media_notifications`` / ``supports_interactive_notifications`` /
    ``supports_location_notifications`` / ``supports_form_notifications``. It advertises
    NO template capability (``supports_template_notifications`` absent) — a template is a
    vendor construct with no place on a page this plugin renders itself."""

    # The page renders a schema-driven form widget, so the ask_user helper may route
    # a ``form`` delivery here; absent this flag it never would.
    supports_form_delivery: ClassVar[bool] = True
    # notify carries a media card and a tappable option list (reply chips + link
    # actions, flat or sectioned); the central notify_user / conversations-delivery
    # capability guards read these before dispatching. Templates stay unsupported.
    supports_media_notifications: ClassVar[bool] = True
    supports_interactive_notifications: ClassVar[bool] = True
    # notify carries a shared location: the page renders a map-pin element (readable
    # coordinates/name/address + an OpenStreetMap link, no external tiles). Absent this
    # flag the delivery guard would refuse a location part to this channel.
    supports_location_notifications: ClassVar[bool] = True
    # notify also carries an ask-less form (a schema notification): the page renders
    # the same schema-driven widget as a fillable card, and the submission enters the
    # conversation as a guest message through this plugin's own form door.
    supports_form_notifications: ClassVar[bool] = True

    async def deliver(self, delivery: ChannelDelivery) -> None:
        """Reserve the pending-question record, then append the question to the
        recipient's chat transcript.

        The deadline is checked BEFORE any reservation or write: a question already
        past its budget is refused loudly for every format. The record is reserved
        BEFORE the transcript append so the answer door can never race a question the
        store does not yet know; a failed OR CANCELLED append releases the
        reservation and re-raises. Every format is appended as-is — the transcript
        entry carries the format and options, and the chat page renders the widget;
        the ``form`` question also carries its answer schema, and only the
        ``external`` widget carries the callback ticket it must open. Any display
        ``media`` rides the same frame in order and renders through the same
        media-card component the notify path uses — display-only, never answered.
        """
        if math.ceil((delivery.timeout_at - datetime.now(UTC)).total_seconds()) <= 0:
            raise ChannelDeliveryError(
                f"interaction {delivery.interaction_id} already timed out "
                f"(timeout_at={delivery.timeout_at.isoformat()}); nothing was sent"
            )

        identity, address = _split_recipient(delivery.recipient)
        record = QuestionRecord(
            callback_url=delivery.callback_url,
            identity=identity,
            address=address,
            timeout_at=delivery.timeout_at,
        )
        await reserve_question(delivery.interaction_id, record)
        appended = False
        try:
            async with transcript_order(identity, address):
                await append_question(
                    identity,
                    address,
                    delivery.interaction_id,
                    delivery.question,
                    delivery.answer_format,
                    delivery.options,
                    delivery.timeout_at,
                    callback_url=(delivery.callback_url if delivery.answer_format == _EXTERNAL_FORMAT else None),
                    schema=delivery.schema,
                    # Display media rides the question frame in order, in the same
                    # shape the media-card renders — an ``image`` inline, a ``link``
                    # as a safe anchor; display-only, never part of the answer.
                    media=(
                        [_media_frame_item(item) for item in delivery.media] if delivery.media is not None else None
                    ),
                )
                # Set INSIDE the gate: leaving the ``async with`` is a suspension
                # point, and a cancellation delivered there would release the
                # reservation for a question already in the transcript — the visitor
                # would see the ask and their answer would 404.
                appended = True
        finally:
            if not appended:
                # The append failed or was cancelled — free the reservation instead
                # of holding it until its TTL, so the interaction times out cleanly.
                # A release that itself fails is logged, never raised: it would
                # replace the error that brought us here with a second one.
                try:
                    await _release_shielded(delivery.interaction_id)
                except Exception:
                    logger.exception(
                        "could not release the reservation for the pending question %s after a failed append; "
                        "it will expire on its own TTL",
                        delivery.interaction_id,
                    )

    async def notify(self, notification: ChannelNotification) -> list[str]:
        """Append one agent entry to the recipient's transcript and return its minted id.

        The transcript pair comes from whichever addressing the caller used. With
        ``sender_identity`` set (the conversation bridge) it IS the web route identity
        and ``recipient`` is the bare visitor id. Without it (the ``notify_user``
        door, which never sets that bridge-owned field) the identity rides in
        ``recipient`` as the composite ``"<identity>:<visitor-id>"``, exactly as a
        delivery addresses one. Both require a recipient — this channel has no
        default.

        A plain text-only notification lands as a ``chat.message`` entry. A notification
        carrying ANY card content — ``media``, ``options``, ``sections``, ``header``,
        ``footer`` or ``location`` — lands as ONE ``chat.media`` card entry: the message is
        its text; every media item rides the frame's media list in order (an ``image``
        inline, a ``document`` as a download card, ``video``/``audio`` as native players, a
        ``link`` as a safe anchor); flat ``options`` (reply chips + link-action buttons) OR a
        sectioned reply list ride the card's choice surface; a ``header`` display item sits
        above the body and a ``footer`` as a muted trailing line; and a ``location`` renders
        as a map-pin element. A CONTENT-ONLY notification (blank message carried by media or a
        location) lands as the same card with an empty ``text`` — no text bubble. A ``data:``
        image is refused loudly — the page renders an image only from an absolute ``https``
        source. A template notification is refused loudly — this channel sends no vendor
        templates.

        A ``schema`` notification (an ask-less form) lands as ONE ``chat.form`` card:
        the message is the form's prompt, the schema is the fillable widget, any media
        and any location ride the same card, and the frame carries a server-minted
        submission token. The token's record (the transcript pair, the schema, the
        message) is stored for the transcript TTL, so the card is submittable exactly as
        long as it can replay; the submission door reads it, renders the ``label: value``
        text from the STORED schema, and bridges the values as a guest message.
        """
        if notification.template is not None:
            raise NotImplementedError("web channel sends no vendor templates; template notifications are not supported")
        if notification.sender_identity is None:
            identity, address = _split_recipient(notification.recipient)
        else:
            identity = _canonical_identity(notification.sender_identity)
            address = _require_recipient(notification.recipient, _NO_RECIPIENT)

        frame_media = (
            [_media_frame_item(item) for item in notification.media] if notification.media is not None else None
        )
        frame_location = _location_frame_item(notification.location) if notification.location is not None else None

        if notification.schema is not None:
            # An ask-less form: ONE chat.form card carrying the prompt, the schema and
            # a server-minted submission token; media and/or a location may ride the
            # same card (options/sections are impossible beside a schema by contract).
            # The token record is written BEFORE the frame — the reserve-before-append
            # rule — so the moment the card is replayable the submission door already
            # resolves its token. A record whose frame then failed to land is unreadable
            # (the token never left the server) and ages out on its own TTL.
            token = await store_form_record(
                FormRecord(identity=identity, address=address, schema=notification.schema, message=notification.message)
            )
            async with transcript_order(identity, address):
                entry_id = await append_form(
                    identity, address, notification.message, notification.schema, token, frame_media, frame_location
                )
            return [entry_id]

        # A plain text send carries no card content at all; anything else is a card.
        if (
            notification.media is None
            and notification.options is None
            and notification.sections is None
            and notification.location is None
        ):
            async with transcript_order(identity, address):
                entry_id = await append_message(identity, address, "out", notification.message)
            return [entry_id]

        frame_options = (
            [_option_frame_item(option) for option in notification.options]
            if notification.options is not None
            else None
        )
        frame_sections = (
            [_section_frame_item(section) for section in notification.sections]
            if notification.sections is not None
            else None
        )
        # A header is display media (never a link); footer a short trailing line. Both
        # ride an interactive card (the contract requires options/sections present), so
        # they only ever accompany a choice surface.
        frame_header = _media_frame_item(notification.header) if notification.header is not None else None
        async with transcript_order(identity, address):
            entry_id = await append_media(
                identity,
                address,
                notification.message,
                frame_media,
                frame_options,
                frame_sections,
                frame_header,
                notification.footer,
                frame_location,
            )
        return [entry_id]
