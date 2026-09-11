"""Shared cross-suite test helpers."""

from __future__ import annotations

import asyncio

from tai42_contract.channels import ChannelNotification
from tai42_contract.template import TemplatedText


def inline_templated_text(text: TemplatedText) -> str:
    """The inline body of ``text``, for a renderer fake that resolves nothing stored.

    A stored-``id`` templated text carries ``content is None``, and such a fake cannot
    read that resource; the empty string is the render of a present-but-empty INLINE
    body, so the stored shape fails here instead of passing for an empty render.
    ``AssertionError`` sits outside the render-failure types the condition doors convert
    into a refusal, so it surfaces as a test failure rather than a plausible deny.
    """
    if text.content is None:
        raise AssertionError(f"this renderer fake resolves no stored resource: {text!r}")
    return text.content


class DeliverOnlyChannel:
    """Base for delivery-focused channel fakes: satisfies the full ``Channel``
    protocol by declaring it cannot notify — ``notify`` raises
    ``NotImplementedError``, exactly as the contract prescribes for a channel
    without a notify capability. Subclasses implement ``deliver``."""

    async def notify(self, notification: ChannelNotification) -> list[str]:
        raise NotImplementedError


async def await_add_event(fake_redis, store, timeout: float = 2.0) -> tuple[str, str]:
    """Poll the events stream until an ``interaction.add`` event appears and
    return its ``(interaction_id, group_id)``; fail the test on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for _entry_id, fields in await fake_redis.xrange(store.events_key):
            if fields.get("type") == "interaction.add":
                return fields["interaction_id"], fields["group_id"]
        await asyncio.sleep(0.01)
    raise AssertionError("no interaction.add event was written")
