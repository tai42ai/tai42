"""The inbound-rejection chokepoint: one notice and one platform event per recognised-but-unbridgeable content.

A channel adapter that recognises an inbound content it cannot turn into a turn (an
unmappable media kind, a file over the platform cap, a byte source it could not fetch, an
unsupported content type) calls :func:`notify_inbound_rejected` instead of silently
ack-and-ignoring it: the participant gets one fixed, reason-typed notice and the operator
gets the ``conversations_inbound_rejected`` event.
"""

from __future__ import annotations

import logging

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelNotification
from tai42_contract.conversations import InboundRejectionReason

from tai42_skeleton.channels.inbound_events import emit_event
from tai42_skeleton.channels.participant_notice import send_participant_notice
from tai42_skeleton.conversations import cache as conversations_cache
from tai42_skeleton.conversations.address import canonical_address
from tai42_skeleton.conversations.turn import ConversationRouteResolutionError, _resolve_channel_route
from tai42_skeleton.conversations.turn.outcome import reply_text

logger = logging.getLogger(__name__)

__all__ = ["INBOUND_REJECTED_EVENT_TOPIC", "notify_inbound_rejected"]

# The platform-event topic emitted when an inbound content the channel recognises cannot
# become a turn (an unmappable media kind, a file over the platform cap, a byte source that
# could not be fetched, or an unsupported content type) and the participant is sent one
# generic notice instead. Core states the fact; a deployment wires a hook to decide what an
# operator sees. DISTINCT from the unrouted topic — a reply with no bound route and a
# recognised-but-unbridgeable content are different faults.
INBOUND_REJECTED_EVENT_TOPIC = "conversations_inbound_rejected"

# The one fixed participant notice per rejection reason. Generic and domain-neutral: the
# notice states the fact without echoing the participant's content, and the specific kind
# refused rides the operator event, never the reply text.
_REJECTION_NOTICES: dict[InboundRejectionReason, str] = {
    InboundRejectionReason.UNSUPPORTED_TYPE: "This content type is not supported here.",
    InboundRejectionReason.TOO_LARGE: "That file is too large to accept here.",
    InboundRejectionReason.COULD_NOT_RECEIVE: "That attachment could not be received.",
}


async def notify_inbound_rejected(
    *,
    channel_id: str,
    recipient: str,
    sender_identity: str,
    kind: str,
    reason: InboundRejectionReason,
) -> None:
    """Reply once and record a platform event when a recognised inbound content cannot become a turn.

    The ONE chokepoint a channel adapter calls instead of silently ack-and-ignoring a
    content it recognises but cannot bridge — an unmappable media kind, a file over the
    platform cap, a byte source it could not fetch, or an unsupported content type.
    The notice goes to ``recipient`` over ``channel_id`` from ``sender_identity``, the
    receiving identity that keys the channel route. ``reason`` selects the route's
    ``reply_texts.rejected_<reason>`` member when the route sets it, else the built-in
    :data:`_REJECTION_NOTICES` copy; ``kind`` is a generic, domain-neutral content label
    that rides the operator event, never the participant reply.

    An unknown ``channel_id`` raises :class:`KeyError` from the registry, like a missing
    route on the accept path. A deployment with no durable routing store, or an identity no
    channel route serves, is logged as a warning and gets the built-in notice; any other
    route-resolution fault (a store fault, an inconsistent routing table) propagates before
    anything is sent, as on the accept path.
    The reply is a ``notice`` send record of the participant's conversation, bounded by
    ``delivery_send_timeout_seconds``. The reply send and the event emit are each best-effort: a
    channel-``notify`` fault, a store fault on the notice's own record write (which stops the
    notice before it is sent) or a hooks-manager fault is logged and swallowed so a handled
    rejection never turns into a lost inbound webhook.
    """
    channel = tai42_app.channels.get(channel_id)
    default_notice = _REJECTION_NOTICES[reason]
    if not conversations_cache.get_conversations_manager().durable:
        logger.warning(
            "inbound: no conversation routing store is configured; sending the built-in %r rejection notice "
            "to %s on channel %r",
            reason.value,
            recipient,
            channel_id,
        )
        notice = default_notice
    else:
        try:
            route = await _resolve_channel_route(channel_id, canonical_address(sender_identity))
        except ConversationRouteResolutionError:
            logger.warning(
                "inbound: no conversation route for identity %s on channel %r; "
                "sending the built-in %r rejection notice",
                sender_identity,
                channel_id,
                reason.value,
            )
            notice = default_notice
        else:
            notice = reply_text(route, f"rejected_{reason.value}", default_notice)
    try:
        await send_participant_notice(
            channel,
            channel_id,
            ChannelNotification(message=notice, recipient=recipient, sender_identity=sender_identity),
        )
    except Exception:
        logger.warning(
            "inbound: failed to send the %r rejection notice to %s on channel %r; continuing",
            reason.value,
            recipient,
            channel_id,
            exc_info=True,
        )
    await _emit_inbound_rejected(channel_id=channel_id, recipient=recipient, kind=kind, reason=reason)


async def _emit_inbound_rejected(*, channel_id: str, recipient: str, kind: str, reason: InboundRejectionReason) -> None:
    """Emit the ``conversations_inbound_rejected`` event ONCE for a recognised-but-unbridgeable content.

    Core states the fact; a deployment wires a hook on this topic to decide what an operator
    sees. The payload names the channel, the participant address, the generic ``kind``
    refused and the ``reason`` — no participant content. Best-effort: a hooks-manager failure
    is logged and swallowed so it never fails the inbound webhook.
    """
    payload = {
        "channel": channel_id,
        "client_address": recipient,
        "kind": kind,
        "reason": reason.value,
    }
    await emit_event(
        INBOUND_REJECTED_EVENT_TOPIC,
        payload,
        failure_desc=f"for the rejected inbound on channel {channel_id!r}",
    )
