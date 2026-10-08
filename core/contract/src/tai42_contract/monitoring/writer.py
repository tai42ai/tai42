"""The write/emit half of the monitoring contract.

FAIL-SAFE INVARIANT
-------------------
The EMIT methods (``open_span``, ``start_span``, ``record_span``, ``create_event``,
``update_current_span``, ``trace_attributes``) and the ``Span``-handle methods
(``Span.update``, ``Span.set_trace_metadata``, ``Span.end``) catch their own errors —
they MUST NOT raise into application code. A monitoring outage cannot be allowed to
break a flow, so call sites need NO ``try/except`` around them. Fail-safe never means
silent: every caught failure is logged at ERROR and counted in
``export_health().records_failed``. (The one precondition that DOES raise is
``record_span`` with no ``trace_id`` — a caller bug, not a backend outage.) The
module-level :func:`attribute_run` helper composes ``writer`` over
``trace_attributes``, inheriting that method's guarantee.

The NON-emit methods are NOT fail-safe and propagate errors loudly: ``flush`` /
``shutdown`` (fork-safety must not be silently skipped) and ``disable`` (a backend may
legitimately no-op it — doing nothing successfully — but a real FAILURE inside it must
raise, never be swallowed).

``current_trace_id()`` / ``current_span_id()`` / ``is_recording()`` / ``export_health()``
are queries: they never raise.

MASKING
-------
A writer renders every ``SecretValue`` found anywhere in a record's input, output or
metadata as ``SECRET_PLACEHOLDER``; callers never mask for monitoring.

RESERVED MARKERS
----------------
A recorded value holds a one-key object whose key starts with ``RESERVED_KEY_PREFIX``
only where the writer rendered a marker (a ``{"$tai42_ref": …}`` reference, the
``{"$tai42_unrecorded": true}`` statement, the writer's own byte-string and unencodable
forms). A user value carrying such an object is refused by the writer, so a marker read
back from a backend is never user data.

AMBIENT vs EXPLICIT context
---------------------------
``trace_context`` is OPTIONAL. ``None`` = emit into the CURRENT (ambient) context —
what the no-handle sites use (plain functions with no trace handle). An explicit
``TraceContext`` is passed only when one is held (e.g. a downstream/parent span held by
a run driver).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from tai42_contract.monitoring.models import (
    DEFAULT_LEVEL,
    MonitoringExportHealth,
    MonitoringLevel,
    RecordId,
    RunAttribution,
    Span,
    SpanKind,
    TokenUsage,
    TraceContext,
)

# The stable trace ``name`` every run-attribution stamp carries, so attributed
# runs group under one neutral name regardless of the door that drove them.
RUN_ATTRIBUTION_TRACE_NAME = "run"

# The documented ``metadata`` key a run's ROOT version rides in, so a writer can
# lift it onto the backend's native version DIMENSION rather than leaving it a
# plain metadata attribute. Vendor-neutral: a backend with a native version field
# maps this key onto it; one without keeps it as ordinary
# metadata. Kept as a stable constant so the depositing seam and the writer name the
# SAME key. Only the OUTERMOST attribution stamp sets it, so the value is unambiguous
# (a single root version) no matter how tags accumulate across nested scopes.
RUN_VERSION_METADATA_KEY = "run_version"


@runtime_checkable
class MonitoringWriter(Protocol):
    """Emit traces/spans/events, report export health, and manage lifecycle.

    Every backend implements this face.
    """

    # --- emit (fail-safe: catch + log + count, never raise) -----------------

    def open_span(
        self,
        *,
        name: str,
        kind: SpanKind,
        trace_context: TraceContext | None = None,
        input_: Any = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        start_time: datetime | None = None,
        activate: bool = False,
    ) -> Span:
        """Open a span (started at ``start_time``, else now) and return its handle; ``Span.end`` records it.

        ``activate=True`` makes the span the current span of the calling context until
        ``end()`` restores the previous one (``end()`` must run in the same context).
        ``model`` / ``model_parameters`` carry generation-span detail. Under an active
        ``disable()`` block the handle is a no-op one.
        """
        ...

    def start_span(
        self,
        *,
        name: str,
        kind: SpanKind,
        trace_context: TraceContext | None = None,
        input_: Any = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AbstractContextManager[Span]:
        """Open an activated span for the duration of the block, yielding its handle.

        Equals ``open_span(activate=True)`` with ``end()`` on exit; calling ``end()``
        inside the block is a caller bug. ``model_parameters`` is open-time only;
        ``model`` and ``usage`` can be amended later via ``Span.update``.
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
        input_: Any = None,
        output: Any = None,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
        model: str | None = None,
        usage: TokenUsage | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record an already-closed span with EXPLICIT ``start`` / ``end`` times.

        For a span whose execution window cannot be timed live in-process — work that
        ran across a pause and is reported afterwards. ``trace_context.trace_id`` is
        REQUIRED — the explicit-time path has no ambient context to fall back to, so a
        missing ``trace_id`` RAISES (a caller bug); ``parent_span_id`` nests the span.
        """
        ...

    def create_event(
        self,
        *,
        name: str,
        level: MonitoringLevel = DEFAULT_LEVEL,
        trace_context: TraceContext | None = None,
        input_: Any = None,
        output: Any = None,
        status_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RecordId | None:
        """Record a point-in-time event carrying ``input`` / ``output``.

        Returns the event's :class:`RecordId` so a producer can reference its values;
        ``None`` when disabled, when the writer records nothing, or when the record failed.
        """
        ...

    def update_current_span(
        self,
        *,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        input_: Any = None,
        output: Any = None,
    ) -> None:
        """Amend the CURRENT span without opening a new one.

        Targets the span of the nearest enclosing activated ``open_span`` / ``start_span``.
        ``metadata`` is MERGED key-wise into the metadata given so far (a later value for a
        key wins). Per-generation token/cost is recorded via ``Span.update``.
        """
        ...

    def trace_attributes(
        self,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> AbstractContextManager[None]:
        """Set trace-level name/tags/metadata on every span started inside the block.

        ``user_id`` / ``session_id`` set the backend's native identity dimensions when
        supplied; ``None`` leaves each unset. A ROOT version is carried inside
        ``metadata`` under :data:`RUN_VERSION_METADATA_KEY` (not a discrete parameter) so
        a backend without a native version dimension keeps it as ordinary metadata.
        """
        ...

    # --- queries (never raise) ---------------------------------------------

    def current_trace_id(self) -> str | None:
        """The active ambient trace id, or ``None`` if no trace is active.

        Used to gate conditional emits (``if writer.current_trace_id():``) so none fire
        outside a trace.
        """
        ...

    def current_span_id(self) -> str | None:
        """The current span's id (16 lower-hex), or ``None`` when no span is current."""
        ...

    def is_recording(self) -> bool:
        """Whether this writer records anything; ``False`` lets producers skip all recording work."""
        ...

    def export_health(self) -> MonitoringExportHealth:
        """A snapshot of everything this writer could not deliver; no I/O.

        Counted per PROCESS: a forked child's counters start at zero, so a child never
        re-counts what its parent counted.
        """
        ...

    def set_health_listener(self, listener: Callable[[MonitoringExportHealth], None] | None) -> None:
        """Install (``None`` removes) the listener called with ``export_health()`` after every flush.

        The writer calls ``listener(self.export_health())`` at the end of EVERY ``flush()``
        and ``shutdown()``, on the calling thread, after that call's export and drop
        accounting completed. A raising listener is logged at ERROR and does not fail the
        flush. A writer that records nothing stores the listener and never calls it.
        """
        ...

    # --- suppression (no-op success allowed; real failure raises) ----------

    def disable(self) -> AbstractContextManager[None]:
        """Suppress emission within the block.

        Every recording method honors it. A backend that never emits may no-op it; an
        emitting backend may not.
        """
        ...

    # --- lifecycle (propagate errors loudly) -------------------------------

    def flush(self) -> None:
        """Deliver (or count as failed) every ended record, then call the health listener."""
        ...

    def shutdown(self) -> None:
        """Tear the exporter down so the next use rebuilds clean (fork-safety), then call the health listener.

        Must fully evict the underlying pipeline, not merely flush — a forked child
        inherits dead background threads otherwise.
        """
        ...


def attribute_run(writer: MonitoringWriter, attribution: RunAttribution) -> AbstractContextManager[None]:
    """Return the context manager that stamps ``attribution`` on the ambient trace for the WRAPPED block.

    A free function composing ``writer`` over :meth:`MonitoringWriter.trace_attributes`,
    kept off the Protocol so structural writers conform without inheriting it. The
    caller ENTERS the returned manager AROUND the whole run so the run's spans are
    created INSIDE the attribution scope. It RETURNS the manager rather than
    entering it — a one-shot enter/exit here would tear the scope down before any
    span exists and silently no-op the stamp.

    It does NOT gate on :meth:`MonitoringWriter.current_trace_id`. A stamp deposited
    BEFORE any trace is open is NOT lost: ``trace_attributes`` is context-scoped — a
    conforming backend writes the attributes into the ambient context and lifts them
    onto the trace ROOT when a span is subsequently opened inside the scope.
    Short-circuiting to a ``nullcontext`` when ``current_trace_id()`` is ``None``
    therefore DROPS the attribution on the common case — a door that deposits then
    opens the run's first span — so there is no such guard. This helper is NOT itself
    fail-safe against a non-conforming writer: it
    CALLS ``trace_attributes`` here, so a writer whose signature omits these keywords
    raises a ``TypeError`` at THIS call — before any enter/exit guard inside the writer
    could run — and a writer that raises on enter/exit surfaces likewise. A caller that
    must not be broken by a monitoring-writer fault guards entering the returned manager
    (the skeleton run seams do, logging the fault and continuing the run unattributed).
    """
    return writer.trace_attributes(
        name=RUN_ATTRIBUTION_TRACE_NAME,
        tags=attribution.tags,
        metadata=attribution.metadata,
        user_id=attribution.user_id,
        session_id=attribution.session_id,
    )
