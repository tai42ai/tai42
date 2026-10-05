"""The ``ChannelWithdrawal`` model and the OPTIONAL ``Channel.withdraw`` member convention."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_contract.channels import Channel, ChannelDelivery, ChannelNotification, ChannelWithdrawal


def test_channel_withdrawal_carries_the_id_and_an_optional_recipient():
    w = ChannelWithdrawal(interaction_id="i-1", recipient="+15550001111")
    assert w.interaction_id == "i-1"
    assert w.recipient == "+15550001111"
    # ``recipient`` defaults to None — the delivery named no address, so the plugin's default.
    assert ChannelWithdrawal(interaction_id="i-2").recipient is None


def test_channel_withdrawal_refuses_a_blank_recipient():
    # Non-blank-when-present, exactly as ``ChannelDelivery.recipient``: a set address is a real one.
    with pytest.raises(ValidationError):
        ChannelWithdrawal(interaction_id="i-1", recipient="   ")


def test_channel_withdrawal_is_frozen():
    w = ChannelWithdrawal(interaction_id="i-1")
    with pytest.raises(ValidationError):
        w.interaction_id = "i-2"  # type: ignore[misc]


def test_a_channel_with_and_without_withdraw_are_both_valid_channels():
    # ``withdraw`` is a documented OPTIONAL member, NOT a Protocol method, so declaring it never
    # tightens the runtime structural check and omitting it leaves a valid ``Channel``.
    class _WithWithdraw:
        async def deliver(self, delivery: ChannelDelivery) -> None:
            return None

        async def notify(self, notification: ChannelNotification) -> list[str]:
            return []

        async def withdraw(self, withdrawal: ChannelWithdrawal) -> None:
            return None

    class _WithoutWithdraw:
        async def deliver(self, delivery: ChannelDelivery) -> None:
            return None

        async def notify(self, notification: ChannelNotification) -> list[str]:
            return []

    assert isinstance(_WithWithdraw(), Channel)
    assert isinstance(_WithoutWithdraw(), Channel)
    # The optional-member convention: read defensively off the instance.
    assert getattr(_WithWithdraw(), "withdraw", None) is not None
    assert getattr(_WithoutWithdraw(), "withdraw", None) is None
