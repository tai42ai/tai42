"""Tier 1 of the send-outcome monitoring layer: one structured span per platform send attempt at the send seams.

A single conditional-emit helper, :func:`send_span`, that every send seam wraps its
one ``channel.notify`` / ``channel.deliver`` call in — so the channel plugins stay
dumb typed-error raisers and the span shape (name, kind, metadata, typed-error
detail) lives in ONE place. The span is emitted ONLY when a trace is ambient
(``current_trace_id()``); outside a trace it is a no-op that just runs the call, so a
standalone send (e.g. a webhook-context best-effort notice with no ambient trace) never
fabricates a rootless span. The conditional-emit idiom (span only under an active
trace) is expressed through the tai42 contract writer the skeleton already uses.

The SUCCESS output (the provider message ids) is set by the caller, which alone knows
them; a FAILURE is marked here from the raised exception (level ERROR + the
:func:`~tai42_skeleton.monitoring.span_metadata.error_span_metadata` detail), so no
seam repeats that mapping.

PII: the recipient rides the span INPUT, never the metadata. The writer masks every
``SecretValue``, so a wrapped value is masked; a plain-string recipient still reaches the
(self-hosted) monitoring backend UNREDACTED, exactly as conversation content already does —
recipient redaction is a monitoring-backend-side concern, not something this seam performs.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

from tai42_contract.monitoring import MonitoringLevel, Span, SpanKind

from tai42_skeleton.monitoring import get_monitoring
from tai42_skeleton.monitoring.span_metadata import error_span_metadata

# ``messaging.*`` follow the OpenTelemetry messaging semantic-convention names, so the
# spans read uniformly across channels on the backend.
_MESSAGING_OPERATION_SEND = "send"


def active_trace_id() -> str | None:
    """Return the ambient trace id, or ``None``.

    The guard the send seams gate their tier-2 index write on (an index entry is only useful when a trace
    exists to correlate a later receipt back to).
    """
    return get_monitoring().writer.current_trace_id()


@contextlib.contextmanager
def send_span(
    channel: str,
    *,
    recipient: str | None,
    attempt: int | None = None,
    retry_decision: Callable[[BaseException], bool] | None = None,
) -> Iterator[Span | None]:
    """Wrap ONE send attempt to ``channel`` in a ``send:<channel>`` span, or run it unwrapped when no trace is ambient.

    Yields the open :class:`Span` handle (so the caller can set the success ``output`` —
    the provider message ids it alone knows) inside a trace, or ``None`` outside one. A
    raised exception is marked on the span as ``MonitoringLevel.ERROR`` with the failure
    detail and re-raised unchanged — the caller's own success/retry/failure control flow
    is never altered.

    A retrying seam passes ``attempt`` (the retry ordinal, one span per attempt) together
    with ``retry_decision``, which it calls with the raised exception ONCE per failed
    attempt — whether or not a trace is ambient — and which returns whether another
    attempt follows; that decision is the span's ``retryable``. A single send passes
    neither, and its span carries the error's own classification.
    """
    if (attempt is None) != (retry_decision is None):
        raise ValueError("send_span: attempt and retry_decision are given together")

    def _failure_metadata(exc: BaseException) -> dict[str, Any]:
        retryable = retry_decision(exc) if retry_decision is not None else None
        return error_span_metadata(exc, retryable=retryable)

    if active_trace_id() is None:
        # No ambient trace: emit nothing (a rootless send span would attach to no run),
        # just run the wrapped call — the retrying seam's decision is still made here.
        try:
            yield None
        except Exception as exc:
            if retry_decision is not None:
                retry_decision(exc)
            raise
        return
    metadata: dict[str, Any] = {"messaging.system": channel, "messaging.operation": _MESSAGING_OPERATION_SEND}
    if attempt is not None:
        # ``retry.attempt`` sits OUTSIDE the ``messaging.*`` namespace ON PURPOSE: the OTel
        # messaging semantic conventions define no retry-attempt attribute, so a
        # ``messaging.retry.attempt`` key would falsely imply a standard name. This is a
        # platform-local attribute, named plainly so it reads as one.
        metadata["retry.attempt"] = attempt
    writer = get_monitoring().writer
    # ``recipient`` on the INPUT path only — see the module docstring on why it is not
    # redacted further here.
    with writer.start_span(
        name=f"send:{channel}",
        kind=SpanKind.TOOL,
        input_={"recipient": recipient},
        metadata=metadata,
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.update(level=MonitoringLevel.ERROR, status_message=str(exc), metadata=_failure_metadata(exc))
            raise


__all__ = ["active_trace_id", "send_span"]
