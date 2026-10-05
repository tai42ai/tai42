"""The ONE seam the skeleton crosses into a channel to WITHDRAW a delivered ask.

When the platform tears a channel-delivered ask down — a cancel, a thread or person erase — the
channel that delivered it still holds its medium-side reservation (a single-slot pending
correlation, a per-interaction sidecar). :func:`withdraw_channel_delivery` releases it through the
channel's OPTIONAL ``withdraw`` member so the slot is free the instant the teardown returns, rather
than lingering until the next inbound 404 or the reservation's own TTL. The kill seam
(:mod:`tai42_skeleton.interactions.kill`) is the only caller.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelWithdrawal

from tai42_skeleton.channels.send_span import send_span


async def withdraw_channel_delivery(*, channel: str | None, recipient: str | None, interaction_id: str) -> None:
    """Release whatever ``channel`` reserved for ``interaction_id`` at delivery, under a send span.

    ``channel is None`` — the ask surfaced only in the inbox, so no channel reserved anything:
    return. An unregistered channel RAISES (``tai42_app.channels.get`` propagates ``KeyError``): a
    withdrawal for a channel whose plugin is no longer loaded is an operator error, never a silent
    skip — inside the kill outbox the reaper then retries and gives up loudly at the deadline. A
    channel that declares no ``withdraw`` member keeps no withdrawable state, so there is nothing to
    release: return.

    Otherwise the channel's ``withdraw`` runs inside the same ``send:<channel>`` span a delivery
    gets, so a release is traced like a send; a raise propagates (the kill seam keeps its kill-due
    record and the reaper redelivers the withdrawal idempotently).
    """
    if channel is None:
        return
    channel_obj = tai42_app.channels.get(channel)
    withdraw = getattr(channel_obj, "withdraw", None)
    if withdraw is None:
        return
    # ``withdraw`` is a documented OPTIONAL member, not a Protocol method, so it is read off the
    # instance untyped; cast it to its documented signature.
    withdraw_member = cast("Callable[[ChannelWithdrawal], Awaitable[None]]", withdraw)
    with send_span(channel, recipient=recipient, attempt=1):
        await withdraw_member(ChannelWithdrawal(interaction_id=interaction_id, recipient=recipient))


__all__ = ["withdraw_channel_delivery"]
