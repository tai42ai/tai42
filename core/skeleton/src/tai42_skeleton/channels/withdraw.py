"""The one crossing from the platform into a channel when a delivered question is taken down.

The whole-run kill calls :func:`withdraw_channel_delivery` for the ask it tears down, so the
channel that delivered it releases what it kept for it (its per-address hold, any
per-interaction sidecar) through the channel's optional ``withdraw`` member. The skeleton knows
no channel's keys: it hands over only the interaction id and the address the delivery carried.
"""

from __future__ import annotations

import logging

from tai42_contract.channels import ChannelWithdrawal

from tai42_skeleton.channels.registry import resolve_channel

logger = logging.getLogger(__name__)


async def withdraw_channel_delivery(*, channel: str | None, recipient: str | None, interaction_id: str) -> None:
    """Tell the channel that delivered ``interaction_id`` to release what it kept for it.

    ``channel`` ``None`` means the question surfaced in the inbox only, so no channel holds
    anything. A named channel resolves loudly (an unknown name is a ``ValueError``); a channel
    that declares no ``withdraw`` member keeps nothing withdrawable. Any raise — the resolution
    or the channel's own fault — propagates to the caller.
    """
    if channel is None:
        return
    channel_obj = resolve_channel(channel)
    withdraw = getattr(channel_obj, "withdraw", None)
    if withdraw is None:
        return
    await withdraw(ChannelWithdrawal(interaction_id=interaction_id, recipient=recipient))
    logger.info("withdrew the %s delivery of interaction %s", channel, interaction_id)
