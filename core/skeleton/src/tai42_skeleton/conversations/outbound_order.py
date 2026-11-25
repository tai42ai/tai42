"""The order of outbound sends to one participant on a channel that confirms delivery by receipt.

On such a channel a returned send means the medium ACCEPTED the message, not that the participant
sees it, and the medium may place two accepted messages in either order. So the next send to a
participant holds until the previous one is reported delivered, bounded by
``delivery_receipt_wait_seconds``: past the wait one WARNING names the missing receipt and the send
goes out. A channel that does not declare ``confirms_delivery_by_receipt`` places a message on return
and its sends follow one another at once, with no store traffic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tai42_contract.channels import Channel, ChannelDeliveryError, SendOutcome
from tai42_contract.conversations import DeliveryReceipt

from tai42_skeleton.conversations.send_scope import current_send_scope

if TYPE_CHECKING:
    from tai42_skeleton.conversations.records import ConversationRecordStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Awaited:
    """One outbound message a send waits on: its provider id and the time it was sent."""

    provider_message_id: str
    sent_at: float


@dataclass(frozen=True)
class HoldOutcome:
    """How the awaited messages settled: delivered, failed with the provider's detail, or no receipt in time."""

    delivered: list[str] = field(default_factory=list)
    failed: list[tuple[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def confirms_delivery_by_receipt(channel: Channel) -> bool:
    """Whether ``channel`` declares that a returned send is only accepted, its delivery reported by receipt."""
    return bool(getattr(channel, "confirms_delivery_by_receipt", False))


async def hold_for_receipts(
    store: ConversationRecordStore, channel: str, awaited: list[Awaited], *, recipient: str | None
) -> HoldOutcome:
    """Hold until every awaited message on ``channel`` has a receipt, or the receipt wait after the newest elapses.

    The flags are read once whatever the clock says, so a receipt already there settles the message
    with no wait. A message whose wait already elapsed before the hold began had its full wait and is
    reported missing without a warning; a hold that waited and still misses a receipt logs one
    WARNING naming it. Never raises on a FAILED receipt: it reports it, and the caller decides.
    """
    settings = store.settings
    wait = settings.delivery_receipt_wait_seconds
    deadline = max(item.sent_at for item in awaited) + wait
    pending = list(dict.fromkeys(item.provider_message_id for item in awaited))
    outcome = HoldOutcome()
    waited = False
    while True:
        flags = await store.receipt_flags([(channel, pid) for pid in pending])
        unsettled: list[str] = []
        for pid, flag in zip(pending, flags, strict=True):
            if flag is None:
                unsettled.append(pid)
            elif flag.status is DeliveryReceipt.FAILED:
                outcome.failed.append((pid, flag.errors))
            else:
                outcome.delivered.append(pid)
        pending = unsettled
        if not pending:
            return outcome
        now = time.time()
        if now >= deadline:
            break
        waited = True
        await asyncio.sleep(min(settings.delivery_receipt_poll_seconds, deadline - now))
    outcome.missing.extend(pending)
    if waited:
        logger.warning(
            "conversations: outbound %s on %r%s reported no delivery receipt within %ss; sending the next message "
            "without it",
            pending,
            channel,
            f" to {recipient!r}" if recipient is not None else "",
            f"{wait:g}",
        )
    return outcome


def resumed_previous(
    store: ConversationRecordStore, channel: Channel, *, provider_message_id: str | None
) -> Awaited | None:
    """The caller's own last sent message, already recorded as sent, as the ``own_previous`` a resumed send holds for.

    A resume follows a lapsed claim, which is longer than the receipt wait after the message was sent
    (the send timeout plus the wait stay under the lease), so its wait is over: it is dated one wait
    ago, the hold reads its receipt flag once and nothing waits again. A receipt that landed while the
    send was in flight is kept for the answer retention, so a FAILED one stops the answer exactly as it
    stops an uninterrupted one. ``None`` for a channel that does not confirm by receipt, or when no
    such message exists.
    """
    if provider_message_id is None or not confirms_delivery_by_receipt(channel):
        return None
    return Awaited(provider_message_id, time.time() - store.settings.delivery_receipt_wait_seconds)


async def ordered_send[T: SendOutcome | None](
    store: ConversationRecordStore,
    channel: Channel,
    *,
    channel_name: str,
    our_identity: str,
    client_address: str,
    own_previous: Awaited | None,
    send: Callable[[], Awaitable[T]],
) -> T:
    """Run ``send`` after the participant's previous outbound is delivered, and name its last id as the new previous.

    ``own_previous`` is the caller's own message just before this one (an answer's previous part),
    held for whatever the participant marker names. The marker is the last message any sender sent to
    ``(channel_name, our_identity, client_address)``; a send holds for it too. A FAILED receipt for
    ``own_previous`` stops the sequence: it raises a non-retryable :class:`ChannelDeliveryError`
    carrying the provider's detail, and nothing is sent. A FAILED receipt for another sender's message
    is that sender's failure: logged at INFO, and the send goes out.

    Only the LAST id of the outcome is named: a channel that returns several ids ordered its own
    earlier ones. A store failure raises, as any store call of the send does.
    """
    if not confirms_delivery_by_receipt(channel):
        return await send()
    awaited = [own_previous] if own_previous is not None else []
    marker = await store.previous_outbound(channel_name, our_identity, client_address)
    if marker is not None and (own_previous is None or marker.provider_message_id != own_previous.provider_message_id):
        awaited.append(Awaited(marker.provider_message_id, marker.sent_at))
    if awaited:
        held = await hold_for_receipts(store, channel_name, awaited, recipient=client_address)
        for pid in [*held.delivered, *(pid for pid, _ in held.failed), *held.missing]:
            await store.clear_previous_outbound(channel_name, our_identity, client_address, pid)
        for pid, errors in held.failed:
            if own_previous is not None and pid == own_previous.provider_message_id:
                raise ChannelDeliveryError(
                    f"channel {channel_name!r} reported outbound {pid!r} to {client_address!r} FAILED before the "
                    f"next message was sent: {errors!r}",
                    retryable=False,
                )
            logger.info(
                "conversations: outbound %s on %r to %r, sent before this one by another sender, was reported "
                "FAILED (%r); sending the next message",
                pid,
                channel_name,
                client_address,
                errors,
            )
    outcome = await send()
    if isinstance(outcome, SendOutcome) and outcome.message_ids:
        await store.set_previous_outbound(
            channel_name, our_identity, client_address, outcome.message_ids[-1], time.time()
        )
    return outcome


async def await_delivery(channel: str, provider_message_id: str) -> None:
    """Hold until ``provider_message_id`` on ``channel`` is reported delivered, or the receipt wait elapses.

    The plugin-facing hold between the provider messages of one notification. A FAILED receipt raises
    a non-retryable :class:`ChannelDeliveryError` carrying the provider's detail. Without a
    conversations store there is nothing to wait on: returns at once.

    Inside a platform-bounded send the hold is the platform's own time: it does not spend the send's
    medium bound, and it keeps the claim or record covering the send alive. Outside one (an ask's
    delivery, whose bound is the question's own deadline) it waits within whatever bounds the caller set.
    """
    from tai42_skeleton.conversations import cache
    from tai42_skeleton.conversations.settings import conversations_settings

    if conversations_settings().in_memory:
        return
    store = cache.get_conversations_manager().records
    scope = current_send_scope()
    awaited = [Awaited(provider_message_id, time.time())]
    if scope is None:
        held = await hold_for_receipts(store, channel, awaited, recipient=None)
    else:
        async with scope.platform_wait():
            held = await hold_for_receipts(store, channel, awaited, recipient=None)
    if held.failed:
        pid, errors = held.failed[0]
        raise ChannelDeliveryError(
            f"channel {channel!r} reported outbound {pid!r} FAILED before the next message was sent: {errors!r}",
            retryable=False,
        )


__all__ = [
    "Awaited",
    "HoldOutcome",
    "await_delivery",
    "confirms_delivery_by_receipt",
    "hold_for_receipts",
    "ordered_send",
    "resumed_previous",
]
