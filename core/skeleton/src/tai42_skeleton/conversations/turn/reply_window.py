"""The ``reply_window`` a turn carries: whether the channel lets a free-form reply reach the client now.

A channel whose medium limits free-form replies to a while after the client's last message
reports it through the optional ``Channel.reply_window`` member. The turn reads it once, at the
target seam, and a late reply reads it again at delivery time; both surface it on the generic
``turn`` block. The platform only relays what the channel reports and gates no send on it.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app
from tai42_contract.channels import ReplyWindow

_NO_WINDOW = ReplyWindow(state="none", closes_at=None)


async def read_turn_reply_window(
    channel: str | None, our_identity: str | None, client_address: str | None
) -> ReplyWindow:
    """The reply window of the pair a turn replies on.

    ``none`` when there is no channel (the api door), no client address, or the channel does not
    implement ``reply_window``. Otherwise the channel's own answer for ``(our_identity,
    client_address)``. A channel that is not registered raises the registry's ``KeyError``, a
    member that raises propagates, and a member that returns anything but a :class:`ReplyWindow`
    raises ``TypeError``: the caller turns each into a recorded error outcome.
    """
    if channel is None or client_address is None:
        return _NO_WINDOW
    member = getattr(tai42_app.channels.get(channel), "reply_window", None)
    if member is None:
        return _NO_WINDOW
    window = await member(recipient=client_address, sender_identity=our_identity)
    if not isinstance(window, ReplyWindow):
        raise TypeError(f"channel {channel!r} reply_window returned {type(window).__name__}, not a ReplyWindow")
    return window
