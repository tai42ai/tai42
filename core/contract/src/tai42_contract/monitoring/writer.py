"""The write/emit half of the monitoring contract.

FAIL-SAFE INVARIANT
-------------------
The EMIT methods (``start_span``, ``record_span``, ``create_event``,
``update_current_span``, ``trace_attributes``) and the ``Span``-handle methods
(``Span.update``, ``Span.set_trace_metadata``) catch their own errors and LOG —
they MUST NOT raise into application code. A monitoring outage cannot be allowed
to break a flow. So call sites need NO ``try/except`` around them. (The one
precondition that DOES raise is ``record_span`` with no ``trace_id`` — a caller
bug, not a backend outage.)

The NON-emit methods are NOT fail-safe and propagate errors loudly:
``flush`` / ``shutdown`` (fork-safety must not be silently skipped),
``inject_context``, ``get_monitoring_callbacks`` (returning ``[]`` on failure
would silently drop all tracing), ``scope`` / ``disable`` (a backend may
legitimately no-op these — doing nothing successfully — but a real FAILURE
inside them must raise, never be swallowed: a silently-failed ``scope()``
could cross-project-leak traces).

``current_trace_id()`` is the sole exception: it returns ``None`` and never
raises, since it is a guard query, not an operation — ``None`` safely means
"no active trace, skip the emit".

AMBIENT vs EXPLICIT context
---------------------------
``trace_context`` is OPTIONAL. ``None`` = emit into the CURRENT (ambient OTel)
context — what the no-handle sites use (plain functions with no trace handle).
An explicit ``TraceContext`` is passed only when one is held (e.g. a
downstream/parent span in flows/agents).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from tai42_contract.monitoring.models import (
    DEFAULT_LEVEL,
    MonitoringLevel,
    Span,
    SpanKind,
    TraceContext,
)


@runtime_checkable
class MonitoringWriter(Protocol):
    """Emit traces/spans/events, supply callbacks, and manage lifecycle.

    Every backend implements this face.
    """

    # --- emit (fail-safe: catch + log, never raise) ------------------------

    def start_span(
        self,
        *,
        name: str,
        kind: SpanKind,
        trace_context: TraceContext | None = None,
        input: Any = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AbstractContextManager[Span]:
        """Open a span for the duration of the block, yielding a ``Span`` handle.

        ``model`` / ``model_parameters`` carry GENERATION-span detail.
        ``model_parameters`` is open-time only (known before the call);
        ``model`` and ``usage_details`` can be amended later via the handle's
        ``Span.update``. A no-op under an active ``disable()`` block.
        """
        ...

    def record_span(
        self,
        *,
        name: str,
        kind: SpanKind,
        start: datetime,
        end: datetime,
        trace_context: TraceContext,
        input: Any = None,
        output: Any = None,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
        model: str | None = None,
        usage_details: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record an already-closed span with EXPLICIT ``start`` / ``end`` times.

        For a span whose execution window cannot be timed live in-process —
        work that ran across a pause and is reported afterwards. ``start`` /
        ``end`` are wall-clock ``datetime``; the backend converts as needed.
        ``trace_context.trace_id`` is REQUIRED — the explicit-time path has no
        ambient context to fall back to, so a missing ``trace_id`` RAISES
        (a caller bug); ``parent_span_id`` nests the span. Backend emission
        failures are caught + logged like the other emit methods.
        """
        ...

    def create_event(
        self,
        *,
        name: str,
        level: MonitoringLevel = DEFAULT_LEVEL,
        trace_context: TraceContext | None = None,
        input: Any = None,
        output: Any = None,
        status_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record a point-in-time event, carrying ``input`` / ``output``."""
        ...

    def update_current_span(
        self,
        *,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        output: Any = None,
    ) -> None:
        """Mutate the CURRENT (ambient) span without opening a new one.

        Targets the span opened by the nearest enclosing ``start_span`` block
        (or the callback-handler span if none). Per-generation token/cost is
        recorded via the held ``Span.update`` handle, not here.
        """
        ...

    def trace_attributes(
        self,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AbstractContextManager[None]:
        """Set trace-level name/tags/metadata on the ambient trace for the block."""
        ...

    # --- guard query (returns None, never raises) --------------------------

    def current_trace_id(self) -> str | None:
        """The active ambient trace id, or ``None`` if no trace is active.

        Used to gate conditional emits (``if writer.current_trace_id():``) so
        none fire outside a trace.
        """
        ...

    # --- propagation / callbacks (propagate errors loudly) -----------------

    def inject_context(self, ctx: TraceContext) -> dict[str, Any]:
        """Build the opaque downstream-propagation blob merged into the
        langgraph ``RunnableConfig``. Carries ``ctx.tags`` + ``ctx.metadata``."""
        ...

    def get_monitoring_callbacks(self, ctx: TraceContext) -> list[object]:
        """The LangChain/LangGraph callback handlers appended to the langgraph
        ``config["callbacks"]``. The caller builds ``ctx`` (keeping the
        auto-generated-trace-id fallback); the impl reads its fields to
        construct the vendor handler."""
        ...

    # --- scoping / suppression (no-op success allowed; real failure raises) -

    def scope(self, public_key: str) -> AbstractContextManager[None]:
        """Bind tracing to a project (``public_key``) for the block.

        Mirrors a single-backend multi-project switch. OTel-native backends
        may no-op it; a real failure must raise (cross-project leak risk).
        """
        ...

    def disable(self) -> AbstractContextManager[None]:
        """Suppress emission within the block.

        The backend's tracing-producing methods (and the handlers from
        ``get_monitoring_callbacks``) MUST honor this. A backend that never
        emits may no-op it; an emitting backend may not.
        """
        ...

    # --- lifecycle (propagate errors loudly) -------------------------------

    def flush(self) -> None:
        """Flush any buffered telemetry."""
        ...

    def shutdown(self) -> None:
        """Tear the client down so the next use rebuilds clean (fork-safety).

        Must fully evict the underlying client, not merely flush — a forked
        child inherits dead background threads otherwise.
        """
        ...
