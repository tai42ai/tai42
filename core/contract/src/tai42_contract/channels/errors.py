"""Typed channel errors: delivery failure, permanent input refusal, and answer-forward failure."""

from __future__ import annotations

from tai42_contract.errors import ErrorKind


class ChannelDeliveryError(Exception):
    """Raised by a :class:`Channel` when delivering a question fails.

    Every failure mode — an unreachable medium API, a rejected send, a missing
    credential, a misconfigured recipient — raises this single typed error.
    ``deliver`` NEVER returns a bool and NEVER silently drops a message: an
    undeliverable question is a loud failure, so the only success signal is a
    plain return.

    ``retryable`` classifies the failure for the caller's retry decision: True
    means transient (a medium 5xx, a rate limit, a transport fault or timeout)
    and a fresh attempt may land. It defaults to False — a rejected recipient, a
    bad credential, and any unrecognised fault fail on the first try rather than
    being blind-retried. ``retry_after`` is the seconds the medium asked the
    caller to wait (an HTTP ``Retry-After``, say); meaningful only when
    ``retryable`` is True.
    """

    # A question handed to a channel could not be delivered to the human.
    __tai_error_kind__ = ErrorKind.DELIVERY_FAILED

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class ChannelInputError(Exception):
    """A permanent refusal of the input's shape or content by the channel.

    The input is contract-valid but the medium cannot render it BY NATURE — e.g. a
    ``data:`` image URL to a channel that sends only public ``https`` sources.
    Retrying the same input can never succeed, so this is distinct from
    :class:`ChannelDeliveryError` (a delivery failure the caller may retry): the
    operation door maps it to the client-error (400) class, never the retryable
    503 a transient delivery failure earns.
    """

    # The input's shape/content is contract-valid but unrenderable by nature — a
    # permanent client-side refusal.
    __tai_error_kind__ = ErrorKind.BAD_INPUT


class AnswerForwardError(Exception):
    """The interactions answer door did not accept a forwarded answer on a status the
    shared inbound-answer ladder cannot resolve (401/413/5xx or a transport fault).

    Raised by :meth:`AppChannels.handle_inbound_answer` WITHOUT releasing the
    correlation, so the channel's transport-level retry (the provider's webhook
    redelivery) re-runs the ladder and the answer is never silently lost. A channel
    lets it propagate out of its inbound webhook so the provider redelivers — the
    same loud-failure contract each channel kept when it hand-rolled the ladder.
    """
