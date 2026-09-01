"""The tool-level retry seam: the declared-policy registry + the attempt loop
``run_tool``'s dispatch runs a policy-armed invocation through.

THE SEAM
--------
Retry happens at the host dispatch chokepoint (``ToolBinding._dispatch_tool``),
INSIDE the runs-index record and the attribution stamp and AFTER the
execution-identity authorization and the tool resolution — so a retried call is
ONE logical dispatch: one run-index row (its terminal outcome is the FINAL
result), one authorization decision, one resolved target re-fired per attempt.
Every consumer of the seam (flows, agents-as-tools, hooks, backend workers,
direct calls) shares the behavior because they all dispatch through
``run_tool``. The langchain client-tool adapter (an in-graph agent's own tool
call) sits deliberately OUTSIDE this seam: there a tool failure surfaces to the
model as a ``ToolException`` and the agent loop owns the recovery decision —
an automatic retry would silently rewrite what the model observed.

OPT-IN, ALLOWLIST, BOUNDED
--------------------------
No declared policy means ``dispatch_with_retry`` awaits the attempt once and
adds NOTHING — no span, no sleep, no wrapper semantics — the byte-identical
no-policy guarantee. With a policy, only an error the classification door
admits is retried (:func:`_retry_delay`): an explicit boolean ``retryable``
verdict on the error wins in both directions (the ``ChannelDeliveryError``
shape), otherwise the error's :class:`~tai42_contract.errors.ErrorKind` must
sit in the policy's declared allowlist — an UNKNOWN/unclassified error is never
retried. Attempts are capped by the policy; the wait is the exponential backoff
widened to a server-provided ``retry_after`` when the error carries one (the
channel-delivery loop's precedent — the medium's ask wins, never narrows), that
honored ask itself ceilinged so a downstream's runaway Retry-After can't park a
detached dispatch across its whole attempt budget.
Cancellation and any other ``BaseException`` propagate immediately, unretried.

COMPOSITION WITH BODY-INTERNAL RETRY
------------------------------------
A body that loops attempts itself (the channel delivery seams) composes by
DECLARATION OWNERSHIP: undeclared, this seam adds zero outer attempts however
transient the terminal error is typed; declared, the outer attempts are the
author's explicit assertion on top of the internal budget (see the
``ToolRetryPolicy`` contract docs).

MONITORING
----------
Each attempt of a policy-armed dispatch is wrapped in a ``tool-attempt:<name>``
span stamping ``retry.attempt`` / ``retry.max_attempts`` — the ``send_span``
per-attempt idiom — emitted only when a trace is ambient (never a rootless
span). A failed attempt's span is ERROR-marked with the typed failure detail;
the spans carry no input/output (the run's existing seams already record the
payloads), only the retry mechanics.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

from tai42_contract.errors import ErrorKind, error_kind
from tai42_contract.monitoring import MonitoringLevel, SpanKind
from tai42_contract.tools import DEFAULT_RETRYABLE_KINDS, NEVER_RETRYABLE_KINDS, ToolRetryPolicy

logger = logging.getLogger(__name__)

# The loop's wait primitive, held as a module attribute so a test can capture
# the backoff schedule without wall-clock sleeps.
_sleep = asyncio.sleep

# A ceiling on how long a server-supplied ``retry_after`` may park one attempt.
# The medium's ask widens the wait, but a detached dispatch (a backend worker
# with no turn budget) running near ``max_attempts`` must not honor a
# downstream's hour-scale Retry-After repeatedly — that would strand the worker
# for the better part of a day on a single stuck call. Five minutes still
# honors a genuine "come back shortly" while bounding the pathological (or
# hostile) header; a caller with a tighter budget cancels sooner, and that
# cancellation propagates unretried. Only the server's ask is bounded here —
# the backoff's own ``cap_seconds`` is the author's explicit choice, untouched.
_MAX_RETRY_AFTER_SECONDS = 300.0


class ToolRetryRegistry:
    """The process-wide per-tool retry-policy registry — the body behind a base
    tool's ``retry`` declaration on ``@app.tools.tool``.

    Reset on every ``start()`` (like the tool-references registry) so a reload
    re-imports the tool modules and re-registers cleanly; a duplicate name within
    one load raises loudly (a silent overwrite could swap a tool's declared
    idempotency claim out from under it)."""

    def __init__(self) -> None:
        self._policies: dict[str, ToolRetryPolicy] = {}

    def register(self, name: str, policy: ToolRetryPolicy) -> None:
        if name in self._policies:
            raise ValueError(f"retry policy for tool {name!r} is already registered")
        self._policies[name] = policy

    def get(self, name: str) -> ToolRetryPolicy | None:
        return self._policies.get(name)

    def reset(self) -> None:
        self._policies.clear()


def _explicit_retry_verdict(exc: BaseException) -> bool | None:
    """The error's OWN boolean ``retryable`` verdict (the ``ChannelDeliveryError``
    shape), or ``None`` when it carries none. Only a real bool counts — any other
    value on the attribute is no verdict, never a truthy accident."""
    verdict = getattr(exc, "retryable", None)
    return verdict if isinstance(verdict, bool) else None


def _declared_retry_after(exc: BaseException) -> float | None:
    """The seconds the server asked the caller to wait, when the error carries a
    positive numeric ``retry_after`` — else ``None``."""
    value = getattr(exc, "retry_after", None)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def _kind_allowlist(policy: ToolRetryPolicy) -> frozenset[ErrorKind]:
    """The error kinds ``policy`` admits for kind-based retry: the contract's
    default transient set for ``retryable=True``, nothing for ``retryable=False``,
    the declared tuple otherwise (the model already rejected the never-retryable
    kinds at declaration; subtracting them keeps the door honest regardless)."""
    if policy.retryable is True:
        return DEFAULT_RETRYABLE_KINDS
    if policy.retryable is False:
        return frozenset()
    return frozenset(policy.retryable) - NEVER_RETRYABLE_KINDS


def _retry_delay(exc: Exception, policy: ToolRetryPolicy, attempt: int) -> float | None:
    """Seconds to wait before re-firing a failed attempt, or ``None`` when it must
    not be retried: a non-idempotent policy (the double-send belt behind the
    model's structural guard), a spent attempt budget, or an error the
    classification door refuses — an explicit ``retryable=False`` verdict, or no
    verdict and a kind outside the policy's allowlist. The wait is the capped
    exponential backoff, widened to the error's ``retry_after`` when the server
    asked for longer (widen-only: a shorter ask never narrows the backoff), the
    honored ``retry_after`` itself bounded by :data:`_MAX_RETRY_AFTER_SECONDS` so
    a downstream's runaway ask can't park a detached dispatch."""
    if not policy.idempotent:
        return None
    if attempt >= policy.max_attempts:
        return None
    verdict = _explicit_retry_verdict(exc)
    if verdict is False:
        return None
    if verdict is None and error_kind(exc) not in _kind_allowlist(policy):
        return None
    backoff = policy.backoff
    delay = min(backoff.cap_seconds, backoff.initial_seconds * backoff.multiplier ** (attempt - 1))
    retry_after = _declared_retry_after(exc)
    if retry_after is None:
        return delay
    return max(delay, min(retry_after, _MAX_RETRY_AFTER_SECONDS))


def _attempt_error_metadata(exc: BaseException) -> dict[str, Any]:
    """The structured failure detail stamped on a failed attempt's span: the
    exception type, its resolved ``error.kind``, and — when the error vouches its
    own verdict — the ``retryable``/``retry_after`` pair (the ``send_span``
    metadata shape, attribute-generic)."""
    metadata: dict[str, Any] = {"error.type": type(exc).__name__, "error.kind": error_kind(exc).value}
    verdict = _explicit_retry_verdict(exc)
    if verdict is not None:
        metadata["retryable"] = verdict
        retry_after = _declared_retry_after(exc)
        if retry_after is not None:
            metadata["retry_after"] = retry_after
    return metadata


@contextlib.contextmanager
def _attempt_span(tool_name: str, attempt: int, max_attempts: int) -> Iterator[None]:
    """Wrap ONE attempt of a policy-armed dispatch in a ``tool-attempt:<name>``
    span, or run it unwrapped when no trace is ambient (a rootless attempt span
    would attach to no run — the ``send_span`` conditional-emit idiom). A raised
    exception marks the span ERROR with the typed detail and propagates
    unchanged; the retry decision is never made here."""
    from tai42_skeleton.monitoring import get_monitoring

    writer = get_monitoring().writer
    if writer.current_trace_id() is None:
        yield
        return
    with writer.start_span(
        name=f"tool-attempt:{tool_name}",
        kind=SpanKind.TOOL,
        metadata={"retry.attempt": attempt, "retry.max_attempts": max_attempts},
    ) as span:
        try:
            yield
        except Exception as exc:
            span.update(level=MonitoringLevel.ERROR, status_message=str(exc), metadata=_attempt_error_metadata(exc))
            raise


async def dispatch_with_retry(
    tool_name: str, policy: ToolRetryPolicy | None, attempt_fn: Callable[[], Awaitable[Any]]
) -> Any:
    """Run one logical tool dispatch under ``policy``: fire ``attempt_fn`` up to
    ``max_attempts`` times, sleeping the classified backoff between attempts and
    propagating the LAST error (honest — never an earlier one replayed) when the
    budget is spent or the failure is not admitted for retry.

    With ``policy=None`` this IS ``await attempt_fn()`` — one attempt, no span,
    no sleep, nothing added: the no-policy byte-identical guarantee. Only an
    ``Exception`` enters the retry decision; ``asyncio.CancelledError`` and any
    other ``BaseException`` propagate immediately."""
    if policy is None:
        return await attempt_fn()
    attempt = 0
    while True:
        attempt += 1
        try:
            with _attempt_span(tool_name, attempt, policy.max_attempts):
                return await attempt_fn()
        except Exception as exc:
            delay = _retry_delay(exc, policy, attempt)
            if delay is None:
                raise
            logger.warning(
                "tool %r attempt %d/%d failed; retrying in %ss",
                tool_name,
                attempt,
                policy.max_attempts,
                delay,
                exc_info=exc,
            )
            await _sleep(delay)


__all__ = ["ToolRetryRegistry", "dispatch_with_retry"]
