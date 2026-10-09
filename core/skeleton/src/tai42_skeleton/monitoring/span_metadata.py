"""The error-metadata shape every failed send or tool-attempt span carries.

Ordinary span metadata: the exception type, its resolved ``error.kind``, the
``retryable`` verdict and a server-requested ``retry_after``.
"""

from __future__ import annotations

from typing import Any

from tai42_contract.channels import ChannelInputError
from tai42_contract.errors import error_kind


def _explicit_retry_verdict(exc: BaseException) -> bool | None:
    """The error's OWN boolean ``retryable`` verdict (the ``ChannelDeliveryError`` shape), or ``None``.

    Only a real bool counts — any other value on the attribute is no verdict, never a truthy accident.
    """
    verdict = getattr(exc, "retryable", None)
    return verdict if isinstance(verdict, bool) else None


def _declared_retry_after(exc: BaseException) -> float | None:
    """The seconds the server asked the caller to wait.

    When the error carries a positive numeric ``retry_after`` — else ``None``.
    """
    value = getattr(exc, "retry_after", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def error_span_metadata(exc: BaseException, *, retryable: bool | None) -> dict[str, Any]:
    """The failure detail stamped on a failed span.

    On a span that carries ``retry.attempt``, ``retryable`` is whether this seam makes
    another attempt; on a single-send span it is the error's own classification.

    ``retryable`` is the deciding seam's verdict when it is a bool. ``None`` (a single
    send, no seam retry) takes the error's own classification: its bool ``retryable``
    attribute, else ``False`` for a :class:`ChannelInputError` (a permanent refusal),
    else no key. ``retry_after`` (the error's positive numeric ask) is stamped only
    alongside ``retryable: True``.
    """
    metadata: dict[str, Any] = {"error.type": type(exc).__name__, "error.kind": error_kind(exc).value}
    verdict = retryable
    if verdict is None:
        verdict = _explicit_retry_verdict(exc)
        if verdict is None and isinstance(exc, ChannelInputError):
            verdict = False
    if verdict is not None:
        metadata["retryable"] = verdict
    if verdict is True:
        retry_after = _declared_retry_after(exc)
        if retry_after is not None:
            metadata["retry_after"] = retry_after
    return metadata


__all__ = ["error_span_metadata"]
