"""Shared cross-suite test helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from tai42_contract.channels import ChannelAddress, ChannelDelivery, ChannelNotification, SendOutcome
from tai42_contract.template import TemplatedText

from tai42_skeleton.app import bus_settings as bus_settings_module
from tai42_skeleton.config.deployment_env import capture_deployment_env, reset_deployment_env


def recapture_deployment_env() -> None:
    """Capture the current ``os.environ`` as the deployment env, replacing the test's own first capture.

    A test that models a name the deployment sets sets it, then calls this.
    """
    reset_deployment_env()
    capture_deployment_env()


@contextmanager
def another_skeleton_major(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Run the block as a skeleton of a major other than the running one, and yield that major.

    Bus settings built inside the block are the settings of a worker of that major, so one
    process can hold workers of two majors on one Redis.
    """
    other = bus_settings_module.skeleton_major() + 1
    with monkeypatch.context() as patch:
        patch.setattr(bus_settings_module, "skeleton_major", lambda: other)
        yield other


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


class AddressingChannel:
    """Base for channel fakes: the ``Channel`` protocol's address side.

    ``resolve_address`` answers the send's own recipient and identity, or the fake's defaults
    when the send names none, and :meth:`outcome` reports a completed send from the same
    address with the provider ids the fake assigned it.
    """

    default_identity = "+15550001111"
    default_recipient = "+15550002222"

    def resolve_address(self, recipient: str | None, sender_identity: str | None) -> ChannelAddress:
        return ChannelAddress(
            our_identity=sender_identity or self.default_identity, address=recipient or self.default_recipient
        )

    def outcome(self, send: ChannelNotification | ChannelDelivery, message_ids: list[str]) -> SendOutcome:
        address = self.resolve_address(send.recipient, getattr(send, "sender_identity", None))
        return SendOutcome(our_identity=address.our_identity, address=address.address, message_ids=message_ids)


class DeliverOnlyChannel(AddressingChannel):
    """Base for delivery-focused channel fakes: satisfies the full ``Channel``
    protocol by declaring it cannot notify — ``notify`` raises
    ``NotImplementedError``, exactly as the contract prescribes for a channel
    without a notify capability. Subclasses implement ``deliver``."""

    async def notify(self, notification: ChannelNotification) -> SendOutcome:
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
