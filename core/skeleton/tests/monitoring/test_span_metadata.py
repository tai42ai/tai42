"""The one error-metadata span shape both retrying seams and every send site stamp."""

from __future__ import annotations

import pytest
from tai42_contract.channels import ChannelDeliveryError, ChannelInputError
from tai42_contract.errors import ErrorKind

from tai42_skeleton.monitoring.span_metadata import error_span_metadata


class _VerdictError(Exception):
    """A neutral error carrying its own retry classification, as any plugin's error may."""

    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR

    def __init__(self, retryable: object, retry_after: object = None) -> None:
        super().__init__("synthetic")
        self.retryable = retryable
        self.retry_after = retry_after


def test_a_delivery_error_gives_its_own_verdict_and_a_positive_retry_after():
    exc = ChannelDeliveryError("503", retryable=True, retry_after=5)
    assert error_span_metadata(exc, retryable=None) == {
        "error.type": "ChannelDeliveryError",
        "error.kind": "delivery_failed",
        "retryable": True,
        "retry_after": 5.0,
    }


@pytest.mark.parametrize(
    "exc",
    [
        ChannelDeliveryError("503", retryable=True, retry_after=0),
        ChannelDeliveryError("503", retryable=True, retry_after=-1),
        ChannelDeliveryError("bad recipient", retryable=False, retry_after=5),
    ],
)
def test_retry_after_is_stamped_only_when_positive_and_retryable(exc: ChannelDeliveryError):
    assert "retry_after" not in error_span_metadata(exc, retryable=None)


def test_an_input_error_is_classified_not_retryable():
    assert error_span_metadata(ChannelInputError("unrenderable"), retryable=None) == {
        "error.type": "ChannelInputError",
        "error.kind": "bad_input",
        "retryable": False,
    }


def test_an_unstamped_exception_gives_the_unknown_kind_and_no_verdict():
    assert error_span_metadata(RuntimeError("boom"), retryable=None) == {
        "error.type": "RuntimeError",
        "error.kind": "unknown",
    }


def test_a_non_bool_retryable_attribute_is_no_verdict():
    assert "retryable" not in error_span_metadata(_VerdictError(retryable="yes"), retryable=None)


def test_the_seam_decision_wins_over_the_error_attribute_both_ways():
    retryable_error = ChannelDeliveryError("503", retryable=True, retry_after=2)
    assert error_span_metadata(retryable_error, retryable=False) == {
        "error.type": "ChannelDeliveryError",
        "error.kind": "delivery_failed",
        "retryable": False,
    }
    permanent_error = _VerdictError(retryable=False, retry_after=3)
    assert error_span_metadata(permanent_error, retryable=True) == {
        "error.type": "_VerdictError",
        "error.kind": "upstream_error",
        "retryable": True,
        "retry_after": 3.0,
    }
    assert error_span_metadata(ChannelInputError("x"), retryable=True)["retryable"] is True
    assert error_span_metadata(RuntimeError("x"), retryable=False)["retryable"] is False
