"""Tests for the typed channel errors: delivery failure vs permanent input refusal."""

from __future__ import annotations

import pytest


def test_delivery_error_is_a_distinct_exception_type():
    from tai42_contract.channels import ChannelDeliveryError

    assert issubclass(ChannelDeliveryError, Exception)
    # A distinct type so the ask helper can catch delivery failure without also
    # swallowing unrelated errors.
    assert ChannelDeliveryError is not Exception
    with pytest.raises(ChannelDeliveryError):
        raise ChannelDeliveryError("send rejected")


def test_input_error_is_distinct_from_delivery_error():
    from tai42_contract.channels import ChannelDeliveryError, ChannelInputError

    assert issubclass(ChannelInputError, Exception)
    # NOT a ChannelDeliveryError: a permanent input refusal must never be caught by a
    # delivery-failure handler and mapped to a retryable 502.
    assert not issubclass(ChannelInputError, ChannelDeliveryError)
    assert not issubclass(ChannelDeliveryError, ChannelInputError)
    with pytest.raises(ChannelInputError):
        raise ChannelInputError("cannot render a data: image URL")


def test_delivery_error_defaults_to_non_retryable():
    from tai42_contract.channels import ChannelDeliveryError

    # An unclassified failure — the message-only form — is never blind-retried.
    error = ChannelDeliveryError("send rejected")
    assert str(error) == "send rejected"
    assert error.retryable is False
    assert error.retry_after is None


def test_delivery_error_carries_the_transient_classification():
    from tai42_contract.channels import ChannelDeliveryError

    error = ChannelDeliveryError("throttled", retryable=True, retry_after=7.5)
    assert str(error) == "throttled"
    assert error.retryable is True
    assert error.retry_after == 7.5
