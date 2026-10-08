"""The ambient in-process trace-context deposit seam.

A driver that dispatches a nested run (a run dispatching a nested-run-as-tool) can
deposit the CURRENT trace's lineage — a :class:`TraceContext` carrying ``trace_id`` and
the anchor ``parent_span_id`` — for the wrapped block; a run-config builder reads it and,
when the caller propagated no explicit :data:`MONITORING_TRACE_ID_KEY` of its own, threads it onto
the nested run so the nested run's spans join the SAME trace instead of minting a fresh,
orphaned one. This module owns that ambient deposit — a ContextVar, mirroring the
run-attribution and session-thread discipline — and the context manager that sets/resets it.

The channel lives in the CONTRACT (not a plugin) on purpose: the depositing driver and the
reading run-config builder sit in different packages that share only
:mod:`tai42_contract`, so the ambient channel must live in the layer both may import. The
contract interprets NOTHING about the deposited context beyond carrying it — a logic-free
channel, exactly like the neutral session-thread channel this package's sibling already
owns.

A task created inside :func:`ambient_trace_context` runs on a COPY and keeps the deposit
for its lifetime, so a detached nested run stays joined to the trace.

TOPOLOGY (the honest nesting anchor)
------------------------------------
The depositor stamps ``parent_span_id`` = the anchor the nested run's root spans nest
under WITHIN the shared trace. At an interrupt-dispatched tool seam the dispatching step's
own span is unusable as that anchor: it closes at the interrupt unwind and the step
re-executes (a DIFFERENT span id) on resume, so nesting the nested run's live spans under
it is temporally incoherent. The chosen anchor is therefore the SAME position the
dispatching run's own tool spans take — the propagated ``parent_span_id`` (the trace ROOT
for a top-level run) — so the nested run's spans sit as honest siblings of the tool span in
the one shared trace, each identifiable by its own name, never dangling under a closed
span.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from tai42_contract.monitoring.models import TraceContext

# The two run-config ``configurable`` keys that carry a run's trace lineage into a nested
# run: the trace to join and the span the nested run's root spans nest under.
MONITORING_TRACE_ID_KEY = "monitoring_trace_id"
MONITORING_PARENT_SPAN_ID_KEY = "monitoring_parent_span_id"

_current_trace_context: ContextVar[TraceContext | None] = ContextVar("tai42_current_trace_context", default=None)


def get_ambient_trace_context() -> TraceContext | None:
    """The trace context deposited for the current run, or ``None`` when none is set."""
    return _current_trace_context.get()


def set_ambient_trace_context(ctx: TraceContext | None) -> Token[TraceContext | None]:
    """Bind ``ctx`` as the current run's ambient trace context.

    Pass the returned token to :func:`reset_ambient_trace_context` to restore the previous
    value.
    """
    return _current_trace_context.set(ctx)


def reset_ambient_trace_context(token: Token[TraceContext | None]) -> None:
    """Restore the ambient trace context to the value captured in ``token``.

    The token comes from the matching :func:`set_ambient_trace_context` call.
    """
    _current_trace_context.reset(token)


@contextmanager
def ambient_trace_context(ctx: TraceContext) -> Generator[None]:
    """Deposit ``ctx`` as the ambient trace context for the wrapped block, resetting it in a ``finally``.

    A task created inside the block inherits it on a copy. Absent this wrap the deposit stays
    ``None`` and a nested run mints its own fresh trace — byte-identical to the pre-deposit
    behavior.
    """
    token = set_ambient_trace_context(ctx)
    try:
        yield
    finally:
        reset_ambient_trace_context(token)


__all__ = [
    "MONITORING_PARENT_SPAN_ID_KEY",
    "MONITORING_TRACE_ID_KEY",
    "ambient_trace_context",
    "get_ambient_trace_context",
    "reset_ambient_trace_context",
    "set_ambient_trace_context",
]
