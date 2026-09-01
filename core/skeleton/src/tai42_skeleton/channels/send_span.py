"""Tier 1 of the send-outcome monitoring layer: one structured span per platform
send attempt at the send seams.

A single conditional-emit helper, :func:`send_span`, that every send seam wraps its
one ``channel.notify`` / ``channel.deliver`` call in — so the channel plugins stay
dumb typed-error raisers and the span shape (name, kind, metadata, typed-error
detail) lives in ONE place. The span is emitted ONLY when a trace is ambient
(``current_trace_id()``); outside a trace it is a no-op that just runs the call, so a
standalone send (e.g. a webhook-context best-effort notice with no ambient trace) never
fabricates a rootless span. The conditional-emit idiom (span only under an active
trace) is expressed through the tai42 contract writer the skeleton already uses.

The SUCCESS output (the provider message ids) is set by the caller, which alone knows
them; a FAILURE is marked here from the raised ``ChannelDeliveryError`` /
``ChannelInputError`` (level ERROR + the typed retry/kind detail), so no seam repeats
that mapping.

PII: the recipient rides the span INPUT, never the metadata. The writer masks ONLY the
input path (``mask_secrets`` unwraps ``SecretValue`` there), so a wrapped value is
masked; a plain-string recipient still reaches the (self-hosted) monitoring backend
UNREDACTED, exactly as conversation content already does — recipient redaction is a
Langfuse-project-side concern, not something this seam performs.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from tai42_contract.channels import ChannelDeliveryError, ChannelInputError
from tai42_contract.monitoring import MonitoringLevel, Span, SpanKind
from tai42_contract.secrets import mask_secrets

from tai42_skeleton.monitoring import get_monitoring

# ``messaging.*`` follow the OpenTelemetry messaging semantic-convention names, so the
# spans read uniformly across channels on the backend.
_MESSAGING_OPERATION_SEND = "send"


def active_trace_id() -> str | None:
    """The ambient trace id, or ``None`` — the guard the send seams gate their
    tier-2 index write on (an index entry is only useful when a trace exists to
    correlate a later receipt back to)."""
    return get_monitoring().writer.current_trace_id()


def _error_metadata(exc: BaseException) -> dict[str, Any]:
    """The structured failure detail lifted off a typed channel error: the exception
    type, its platform ``error.kind`` (``ChannelDeliveryError`` /
    ``ChannelInputError`` both carry ``__tai_error_kind__``), and — for a delivery
    failure — whether it is ``retryable`` and any medium-requested ``retry_after``. An
    input error is never retryable (a permanent shape refusal)."""
    metadata: dict[str, Any] = {"error.type": type(exc).__name__}
    kind = getattr(exc, "__tai_error_kind__", None)
    if kind is not None:
        metadata["error.kind"] = getattr(kind, "value", str(kind))
    if isinstance(exc, ChannelDeliveryError):
        metadata["retryable"] = exc.retryable
        if exc.retry_after is not None:
            metadata["retry_after"] = exc.retry_after
    elif isinstance(exc, ChannelInputError):
        metadata["retryable"] = False
    return metadata


@contextlib.contextmanager
def send_span(channel: str, *, recipient: str | None, attempt: int | None = None) -> Iterator[Span | None]:
    """Wrap ONE send attempt to ``channel`` in a ``send:<channel>`` span, or run it
    unwrapped when no trace is ambient.

    Yields the open :class:`Span` handle (so the caller can set the success ``output`` —
    the provider message ids it alone knows) inside a trace, or ``None`` outside one. A
    raised ``ChannelDeliveryError`` / ``ChannelInputError`` (or any other send exception)
    is marked on the span as ``MonitoringLevel.ERROR`` with the typed failure detail and
    re-raised unchanged — the caller's own success/retry/failure control flow is never
    altered. ``attempt`` stamps the retry ordinal when the seam retries (one span per
    attempt), so a "attempt 1 failed retryable, attempt 2 accepted" sequence is visible
    rather than collapsed."""
    if active_trace_id() is None:
        # No ambient trace: emit nothing (a rootless send span would attach to no run),
        # just run the wrapped call.
        yield None
        return
    metadata: dict[str, Any] = {"messaging.system": channel, "messaging.operation": _MESSAGING_OPERATION_SEND}
    if attempt is not None:
        # ``retry.attempt`` sits OUTSIDE the ``messaging.*`` namespace ON PURPOSE: the OTel
        # messaging semantic conventions define no retry-attempt attribute, so a
        # ``messaging.retry.attempt`` key would falsely imply a standard name. This is a
        # platform-local attribute, named plainly so it reads as one.
        metadata["retry.attempt"] = attempt
    writer = get_monitoring().writer
    # ``recipient`` on the INPUT path only (masked there if a SecretValue) — see the
    # module docstring on why it is not redacted further here.
    with writer.start_span(
        name=f"send:{channel}",
        kind=SpanKind.TOOL,
        input=mask_secrets({"recipient": recipient}),
        metadata=metadata,
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.update(level=MonitoringLevel.ERROR, status_message=str(exc), metadata=_error_metadata(exc))
            raise


__all__ = ["active_trace_id", "send_span"]
