"""A run's one trace lineage: resolve it, and open the ``run`` span that roots it when none exists.

* :func:`resolve_trace_context` reads the lineage a run already has: an explicit pair in its
  config, an explicit parent paired with the ambient deposit's trace, or the ambient deposit.
  It never mints one.
* :func:`begin_run_trace` keeps that lineage, or opens one ``run`` span (a child of the live
  span, or the root of a new trace) and returns a :class:`RunTraceRoot` whose ``step()`` makes
  it current and deposits it for one step of the run. A lineage the config names is made
  current and deposited the same way; one inherited from the ambient deposit stays deposited
  and is made current by nothing here.
* :func:`open_run_trace` holds a run kept in one block inside its lineage.
* :func:`open_lineage_span` opens one named span continuing a lineage handed over explicitly.

A span an opener opens records the run's input (``input_``, given when it opens) and its output
(:meth:`RunTraceRoot.record_output`, given when the run ends), both encoded by the writer as every
other recorded payload. A kept lineage opens no span and records neither: the run is a step of a
trace whose root carries its own input and output.

Every writer member called here is fail-safe, so the openers raise nothing but
:class:`InvalidRunTraceError` for a config they refuse.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.errors import ErrorKind
from tai42_contract.monitoring import (
    MONITORING_PARENT_SPAN_ID_KEY,
    MONITORING_TRACE_ID_KEY,
    RUN_ATTRIBUTION_TRACE_NAME,
    MonitoringLevel,
    MonitoringWriter,
    Span,
    SpanKind,
    TraceContext,
    ambient_trace_context,
    get_ambient_trace_context,
    is_span_id,
    is_trace_id,
)


class InvalidRunTraceError(ValueError):
    """A run's config names a trace id or parent span that no monitoring writer can record under.

    The caller's input is refused before the run starts, naming the configurable key and the value it carried.
    """

    __tai_error_kind__ = ErrorKind.BAD_INPUT


def resolve_trace_context(config: dict[str, Any] | None = None) -> TraceContext | None:
    """The lineage a run already has, from ``config`` and the ambient deposit; ``None`` when it has none.

    An explicit ``configurable[MONITORING_TRACE_ID_KEY]`` needs ``configurable[MONITORING_PARENT_SPAN_ID_KEY]``
    beside it. An explicit parent alone pairs with the ambient deposit's trace. Else the ambient deposit. Pure.

    A trace id that is not 32 lower-hex (not all zeros), a parent that is not 16 lower-hex (not all zeros), a trace
    without a parent, or a parent with no trace to pair with is refused with :class:`InvalidRunTraceError`. An empty
    explicit trace id reads as absent.
    """
    configured = _configured_trace_context(config)
    if configured is not None:
        return configured
    return _ambient_lineage()


def _ambient_lineage() -> TraceContext | None:
    ambient = get_ambient_trace_context()
    return ambient if ambient is not None and ambient.trace_id else None


def _configured_trace_context(config: dict[str, Any] | None) -> TraceContext | None:
    """The lineage ``config`` names: its explicit pair, or its explicit parent paired with the ambient trace."""
    configurable = (config or {}).get("configurable", {})
    trace_id = configurable.get(MONITORING_TRACE_ID_KEY)
    parent_span_id = configurable.get(MONITORING_PARENT_SPAN_ID_KEY)
    if trace_id and not is_trace_id(trace_id):
        raise InvalidRunTraceError(
            f"configurable[{MONITORING_TRACE_ID_KEY!r}] must be a 32-character lowercase hex trace id that is not all "
            f"zeros; got {trace_id!r}"
        )
    if parent_span_id is not None and not is_span_id(parent_span_id):
        raise InvalidRunTraceError(
            f"configurable[{MONITORING_PARENT_SPAN_ID_KEY!r}] must be a 16-character lowercase hex span id that is not "
            f"all zeros; got {parent_span_id!r}"
        )
    if trace_id:
        if parent_span_id is None:
            raise InvalidRunTraceError(
                f"configurable[{MONITORING_TRACE_ID_KEY!r}] names a trace without a parent span: set "
                f"configurable[{MONITORING_PARENT_SPAN_ID_KEY!r}] to the span the run nests under"
            )
        return TraceContext(trace_id=trace_id, parent_span_id=parent_span_id)
    ambient = get_ambient_trace_context()
    if parent_span_id is not None:
        if ambient is None or not ambient.trace_id:
            raise InvalidRunTraceError(
                f"configurable[{MONITORING_PARENT_SPAN_ID_KEY!r}] names a parent span with no trace: set "
                f"configurable[{MONITORING_TRACE_ID_KEY!r}] beside it"
            )
        return TraceContext(trace_id=ambient.trace_id, parent_span_id=parent_span_id)
    return None


class RunTraceRoot:
    """A run's lineage and the ``run`` span it opened, if any.

    ``step()`` makes the span (with no span, the lineage) current and deposits the lineage for one step of the run;
    enter and exit it in the same context, never across a ``yield`` a consumer drives. ``record_output()`` records
    the run's output and ``end()`` ends the span.
    """

    __slots__ = ("_ended", "_span", "_writer", "context")

    def __init__(self, context: TraceContext | None, span: Span | None, writer: MonitoringWriter | None) -> None:
        """Hold ``context`` and the ``span`` that ``writer`` opened for it.

        ``span`` is ``None`` when none was opened; ``writer`` is ``None`` when the step makes nothing current.
        """
        self.context = context
        self._span = span
        self._writer = writer
        self._ended = False

    def step(self) -> AbstractContextManager[None]:
        """The block in which the held span, else the lineage, is current and the lineage deposited.

        A no-op for a lineage inherited from the ambient deposit: it stays deposited and nothing is made current.
        """
        if self._writer is None or self.context is None:
            return nullcontext()
        return self._held_step(self._writer, self._span, self.context)

    @staticmethod
    @contextmanager
    def _held_step(writer: MonitoringWriter, span: Span | None, context: TraceContext) -> Iterator[None]:
        current = writer.use_trace_context(context) if span is None else writer.use_span(span)
        with current, ambient_trace_context(context):
            yield

    def record_output(self, output: Any) -> None:
        """Record ``output`` as the held span's output; a no-op when no span is held or it has ended."""
        if self._span is not None and not self._ended:
            self._span.update(output=output)

    def fail(self, error: Exception) -> None:
        """Record ``error`` on the held span at ``ERROR``."""
        if self._span is not None:
            self._span.update(level=MonitoringLevel.ERROR, status_message=str(error))

    def end(self) -> None:
        """End the held span; a second call does nothing."""
        if self._span is None or self._ended:
            return
        self._ended = True
        self._span.end()


def _open_root(name: str, trace_context: TraceContext | None, input_: Any) -> RunTraceRoot:
    writer = tai42_app.monitoring.active.writer
    if not writer.is_recording():
        return RunTraceRoot(None, None, None)
    span = writer.open_span(name=name, kind=SpanKind.CHAIN, trace_context=trace_context, input_=input_)
    with writer.use_span(span):
        trace_id = writer.current_trace_id()
    if not span.id or not trace_id:
        span.end()
        return RunTraceRoot(None, None, None)
    return RunTraceRoot(TraceContext(trace_id=trace_id, parent_span_id=span.id), span, writer)


def begin_run_trace(config: dict[str, Any] | None = None, *, input_: Any = None) -> RunTraceRoot:
    """Begin a run's one lineage without making anything current.

    A lineage the config or the ambient deposit already names is kept: one the config names is current and
    deposited only inside :meth:`RunTraceRoot.step`, one inherited from the ambient deposit is left as it is.
    Otherwise one ``run`` span opens with ``input_`` as its input, a child of the live span or, with none live, the
    root of a new trace, and is current only inside :meth:`RunTraceRoot.step`. ``context`` is ``None`` when nothing
    records.
    """
    configured = _configured_trace_context(config)
    if configured is not None:
        return RunTraceRoot(configured, None, tai42_app.monitoring.active.writer)
    ambient = _ambient_lineage()
    if ambient is not None:
        return RunTraceRoot(ambient, None, None)
    return _open_root(RUN_ATTRIBUTION_TRACE_NAME, None, input_)


@contextmanager
def _hold(root: RunTraceRoot) -> Iterator[RunTraceRoot]:
    try:
        with root.step():
            yield root
    except Exception as exc:
        root.fail(exc)
        raise
    finally:
        root.end()


@contextmanager
def open_run_trace(config: dict[str, Any] | None = None, *, input_: Any = None) -> Iterator[RunTraceRoot]:
    """Give a run held in one block its one lineage: the root of :func:`begin_run_trace`, current and deposited.

    The block records the run's output on the yielded root; a raise is recorded on it before it ends. Never hold
    it across a ``yield`` a consumer drives; an async generator uses :func:`begin_run_trace` and binds ``step()``
    per step.
    """
    with _hold(begin_run_trace(config, input_=input_)) as root:
        yield root


@contextmanager
def open_lineage_span(name: str, *, parent: TraceContext, input_: Any = None) -> Iterator[RunTraceRoot]:
    """Open one span named ``name`` with ``input_`` as a child of ``parent``, current and deposited for the block.

    The block records its output on the yielded root; the span ends on exit.
    """
    with _hold(_open_root(name, parent, input_)) as root:
        yield root


__all__ = [
    "InvalidRunTraceError",
    "RunTraceRoot",
    "begin_run_trace",
    "open_lineage_span",
    "open_run_trace",
    "resolve_trace_context",
]
