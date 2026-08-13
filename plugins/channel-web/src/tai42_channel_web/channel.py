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

This channel advertises no media/template capability: every notification is plain
text, and the central ``notify_user`` guard refuses a media/template send before it
reaches here; the defensive guard below refuses one loudly if it ever arrives.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime
from typing import ClassVar

from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelNotification

from tai42_channel_web.store import (
    QuestionRecord,
    append_message,
    append_question,
    release_question,
    reserve_question,
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
    form widget, so a ``form`` question is delivered here. It advertises no
    notification capability: ``notify`` sends plain text only (no media, no
    templates)."""

    # The page renders a schema-driven form widget, so the ask_user helper may route
    # a ``form`` delivery here; absent this flag it never would.
    supports_form_delivery: ClassVar[bool] = True

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
        ``external`` widget carries the callback ticket it must open.
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
        """Append one ``chat.message`` agent entry to the recipient's transcript and
        return its minted id.

        The transcript pair comes from whichever addressing the caller used. With
        ``sender_identity`` set (the conversation bridge) it IS the web route identity
        and ``recipient`` is the bare visitor id. Without it (the ``notify_user``
        door, which never sets that bridge-owned field) the identity rides in
        ``recipient`` as the composite ``"<identity>:<visitor-id>"``, exactly as a
        delivery addresses one. Both require a recipient — this channel has no
        default. A media or template notification is refused loudly — this channel
        sends plain text only.
        """
        if notification.media is not None or notification.template is not None:
            raise ChannelDeliveryError("web channel sends plain text only; media and template are not supported")
        if notification.sender_identity is None:
            identity, address = _split_recipient(notification.recipient)
        else:
            identity = _canonical_identity(notification.sender_identity)
            address = _require_recipient(notification.recipient, _NO_RECIPIENT)
        async with transcript_order(identity, address):
            entry_id = await append_message(identity, address, "out", notification.message)
        return [entry_id]
