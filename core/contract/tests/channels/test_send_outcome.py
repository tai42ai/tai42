"""Tests for ``SendOutcome`` and ``ChannelAddress`` — what a send reports and where it would go."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_send_outcome_is_frozen_and_keeps_ids_in_order():
    from tai42_contract.channels import SendOutcome

    outcome = SendOutcome(our_identity="bot-1", address="12345", message_ids=["m1", "m2"])
    assert outcome.message_ids == ["m1", "m2"]
    with pytest.raises(ValidationError):
        outcome.address = "other"


def test_channel_address_is_frozen():
    from tai42_contract.channels import ChannelAddress

    address = ChannelAddress(our_identity="bot-1", address="@handle")
    assert (address.our_identity, address.address) == ("bot-1", "@handle")
    with pytest.raises(ValidationError):
        address.address = "other"


@pytest.mark.parametrize("model_name", ["SendOutcome", "ChannelAddress"])
@pytest.mark.parametrize("field", ["our_identity", "address"])
def test_addresses_are_bounded_and_non_blank(model_name: str, field: str):
    import tai42_contract.channels as channels
    from tai42_contract.channels import NOTIFICATION_ADDRESS_MAX_CHARS

    model = getattr(channels, model_name)
    base = {"our_identity": "bot-1", "address": "12345"}
    if model_name == "SendOutcome":
        base["message_ids"] = []  # type: ignore[assignment]
    with pytest.raises(ValidationError, match="at most"):
        model(**{**base, field: "x" * (NOTIFICATION_ADDRESS_MAX_CHARS + 1)})
    with pytest.raises(ValidationError, match="non-empty"):
        model(**{**base, field: "  "})
    assert getattr(model(**{**base, field: "x" * NOTIFICATION_ADDRESS_MAX_CHARS}), field)


def test_channel_address_error_is_a_non_retryable_delivery_error():
    from tai42_contract.channels import ChannelAddressError, ChannelDeliveryError

    error = ChannelAddressError("recipient 'x' is not on the allowlist")
    assert isinstance(error, ChannelDeliveryError)
    assert error.retryable is False
    assert str(error) == "recipient 'x' is not on the allowlist"
