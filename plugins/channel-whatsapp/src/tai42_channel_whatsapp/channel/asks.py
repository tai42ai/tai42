"""Question-ask rendering: the Tier-1 callback link and the Tier-2 text/select send."""

from __future__ import annotations

from tai42_contract.channels import ChannelDelivery

from tai42_channel_whatsapp.channel.interactive import _choice_of, _send_choice
from tai42_channel_whatsapp.client import send_message

# The DEFAULT line carrying the tappable answer link; the caller's own ``wording.lines.answer_link``
# overrides it per send. Its ``{url}`` token is filled with the callback link by a plain substitution.
_ANSWER_LINK_LINE = "Answer here: {url}"


def _render_link(delivery: ChannelDelivery) -> str:
    """The message body for a Tier-1 ask (``confirm`` or ``external``).

    The question plus the tappable callback link — the caller's ``wording.lines.answer_link`` (its
    ``{url}`` token filled with the callback link) when set, else the generic :data:`_ANSWER_LINK_LINE`.
    """
    lines = delivery.wording.lines if delivery.wording is not None else None
    template = lines.answer_link if lines is not None else None
    link_line = (template or _ANSWER_LINK_LINE).replace("{url}", delivery.callback_url)
    return f"{delivery.question}\n\n{link_line}"


def _interaction_ids(delivery: ChannelDelivery) -> list[tuple[str, str]]:
    """``(id, title)`` per option, id = ``{interaction_id}:{index}`` (0-based).

    The index binds the tap to the exact ask: the inbound handler requires the
    id's interaction part to equal the pending ask's before mapping the index to
    ``options[index]``.
    """
    return [(f"{delivery.interaction_id}:{index}", option) for index, option in enumerate(delivery.options or [])]


async def _send_question(phone_number_id: str, target: str, delivery: ChannelDelivery) -> None:
    """Push a Tier-2 question to the target in its native shape."""
    if delivery.answer_format != "select":
        await send_message(phone_number_id=phone_number_id, to=target, body=delivery.question)
        return
    await _send_choice(
        phone_number_id,
        target,
        delivery.question,
        delivery.options or [],
        _interaction_ids(delivery),
        _choice_of(delivery),
    )
