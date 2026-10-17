"""Tests for ``ChannelWithdrawal`` — one delivered question the platform has taken down."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_withdrawal_is_exported_from_the_channels_package():
    import tai42_contract.channels as channels
    from tai42_contract.channels.withdrawal import ChannelWithdrawal

    assert channels.ChannelWithdrawal is ChannelWithdrawal
    assert "ChannelWithdrawal" in channels.__all__


def test_withdrawal_carries_the_id_and_the_address():
    from tai42_contract.channels import ChannelWithdrawal

    withdrawal = ChannelWithdrawal(interaction_id="int-1", recipient="alice")
    assert withdrawal.interaction_id == "int-1"
    assert withdrawal.recipient == "alice"


def test_withdrawal_recipient_defaults_to_none():
    from tai42_contract.channels import ChannelWithdrawal

    assert ChannelWithdrawal(interaction_id="int-1").recipient is None
    assert ChannelWithdrawal(interaction_id="int-1", recipient=None).recipient is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_withdrawal_refuses_a_blank_recipient(blank: str):
    from tai42_contract.channels import ChannelWithdrawal

    with pytest.raises(ValidationError, match="recipient"):
        ChannelWithdrawal(interaction_id="int-1", recipient=blank)


def test_withdrawal_is_frozen():
    from tai42_contract.channels import ChannelWithdrawal

    withdrawal = ChannelWithdrawal(interaction_id="int-1", recipient="alice")
    with pytest.raises(ValidationError):
        withdrawal.recipient = "bob"  # type: ignore[misc]
