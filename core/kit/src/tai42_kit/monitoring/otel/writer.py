"""``OtelWriter``: the one ``MonitoringWriter`` every monitoring backend records through.

Its own ``TracerProvider`` (never the global one), a ``BatchSpanProcessor`` exporting
OTLP/HTTP to a collector, and its own ``MeterProvider`` reading the processor's queue-full
count. On the caller's thread a record costs one ``encode_payload`` per value and the
attribute writes; protobuf encoding and the network run on the processor's worker thread.

Configuration is the OpenTelemetry SDK's own standard environment (``OTEL_EXPORTER_OTLP_*``,
``OTEL_BSP_*``, ``OTEL_SERVICE_NAME``, ``OTEL_RESOURCE_ATTRIBUTES``,
``OTEL_METRIC_EXPORT_INTERVAL``). Attribute value length is explicitly unlimited, so no
environment can truncate a recorded value.

Fork safety: the counters are per process. A forked child resets the writer before any use
(an at-fork hook AND a pid check at the top of every public method, whichever runs first),
so it starts from zero counters and builds its own pipeline; the inherited pipeline is kept
referenced and never used.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import weakref
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import orjson
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, Tracer, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, Status, StatusCode, TraceFlags
from tai42_contract.monitoring import (
    DEFAULT_LEVEL,
    PROMOTED_METADATA_KEYS,
    MonitoringExportHealth,
    MonitoringLevel,
    RecordId,
    Span,
    SpanKind,
    TokenUsage,
    TraceContext,
)

from tai42_kit.monitoring.encode import UNENCODABLE_KEY, encode_payload
from tai42_kit.monitoring.otel import attributes as attr
from tai42_kit.monitoring.otel.attribution import (
    AttributionFrame,
    AttributionSpanProcessor,
    merged_attributes,
    pushed_frame,
)
from tai42_kit.monitoring.otel.exporter import CountingSpanExporter, QueueDropMetricExporter
from tai42_kit.monitoring.otel.health import ExportHealthCounters
from tai42_kit.monitoring.otel.ids import ChosenTraceIdGenerator, chosen_trace_id

logger = logging.getLogger(__name__)

TRACER_NAME = "tai42.monitoring"
_INTERNAL_METRICS_ENV = "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED"
_ENDPOINT_ENVS = ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")
# The writer sets at most ~25 attributes per span; the bound only makes an eviction
# impossible in practice (any eviction is still counted by the exporter).
_MAX_SPAN_ATTRIBUTES = 128

_DISABLED: ContextVar[bool] = ContextVar("tai42_otel_disabled", default=False)

HealthListener = Callable[[MonitoringExportHealth], None]


def _ns(ts: datetime | None) -> int | None:
    return None if ts is None else int(ts.timestamp() * 1_000_000_000)


def _hex_ids(span_context: SpanContext) -> tuple[str, str]:
    return format(span_context.trace_id, "032x"), format(span_context.span_id, "016x")


_HEX = {32: re.compile(r"[0-9a-fA-F]{32}"), 16: re.compile(r"[0-9a-fA-F]{16}")}


def _parse_hex(value: str, width: int) -> int:
    if not _HEX[width].fullmatch(value):
        raise ValueError(f"expected {width} hex characters, got {value!r}")
    parsed = int(value, 16)
    if parsed == 0:
        raise ValueError("an all-zero id is invalid")
    return parsed


@dataclass(slots=True)
class _Pipeline:
    provider: TracerProvider
    meter_provider: MeterProvider
    tracer: Tracer


class _NoOpSpan:
    """The handle of a record that is not written (disabled, or its setup failed); ``id`` is ``""``."""

    @property
    def id(self) -> str:
        return ""

    def update(
        self,
        *,
        output: Any = None,
        model: str | None = None,
        usage: TokenUsage | None = None,
        metadata: dict[str, Any] | None = None,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
    ) -> None:
        pass

    def set_trace_metadata(self, *, name: str | None = None, tags: list[str] | None = None) -> None:
        pass

    def end(self, *, end_time: datetime | None = None) -> None:
        pass


_NOOP_SPAN = _NoOpSpan()


class _OtelSpan:
    """The handle of one open span. Metadata is merged here and written once, at ``end()``."""

    __slots__ = (
        "_activation",
        "_ended",
        "_handle_token",
        "_kind",
        "_metadata",
        "_otel",
        "_previous_handle",
        "_previous_span",
        "_writer",
    )

    def __init__(self, writer: OtelWriter, otel_span: otel_trace.Span, kind: SpanKind) -> None:
        self._writer = writer
        self._otel = otel_span
        self._kind = kind
        self._metadata: dict[str, Any] = {}
        self._activation: Token[Context] | None = None
        self._handle_token: Token[_OtelSpan | None] | None = None
        self._previous_handle: _OtelSpan | None = None
        self._previous_span: otel_trace.Span = otel_trace.INVALID_SPAN
        self._ended = False

    @property
    def id(self) -> str:
        return format(self._otel.get_span_context().span_id, "016x")

    @property
    def name(self) -> str:
        return getattr(self._otel, "name", "")

    @property
    def ended(self) -> bool:
        return self._ended

    def activate(self) -> None:
        self._previous_handle = _ACTIVE_HANDLE.get()
        self._previous_span = otel_trace.get_current_span()
        self._activation = otel_context.attach(otel_trace.set_span_in_context(self._otel))
        self._handle_token = _ACTIVE_HANDLE.set(self)

    def set_input(self, value: Any) -> None:
        self._otel.set_attribute(attr.input_attribute(self._kind), self._writer.encode_field(value, self.name))

    def merge_metadata(self, metadata: Mapping[str, Any]) -> None:
        self._metadata.update(metadata)

    def update(
        self,
        *,
        output: Any = None,
        model: str | None = None,
        usage: TokenUsage | None = None,
        metadata: dict[str, Any] | None = None,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
    ) -> None:
        try:
            if output is not None:
                self._otel.set_attribute(
                    attr.output_attribute(self._kind), self._writer.encode_field(output, self.name)
                )
            if model is not None:
                self._otel.set_attribute(attr.GEN_AI_RESPONSE_MODEL, model)
            if usage is not None:
                self._otel.set_attributes(_usage_attributes(usage))
            if metadata:
                self._metadata.update(metadata)
            _apply_level(self._otel, level, status_message)
        except Exception as exc:
            self._writer.record_failure("Span.update", self.name, exc)

    def set_trace_metadata(self, *, name: str | None = None, tags: list[str] | None = None) -> None:
        try:
            if name is not None:
                self._otel.set_attribute(attr.TRACE_NAME, name)
            if tags is not None:
                self._otel.set_attribute(attr.TRACE_TAGS, self._writer.encode_field(list(tags), self.name))
        except Exception as exc:
            self._writer.record_failure("Span.set_trace_metadata", self.name, exc)

    def end(self, *, end_time: datetime | None = None) -> None:
        self.finish(_ns(end_time))

    def finish(self, end_ns: int | None) -> bool:
        """Write the merged metadata and end the span at ``end_ns`` (now when ``None``); whether it was written."""
        if self._ended:
            return False
        self._ended = True
        try:
            self._otel.set_attributes(self._writer.metadata_attributes(self._metadata, self.name))
            self._otel.end(end_time=end_ns)
        except Exception as exc:
            self._writer.record_failure("Span.end", self.name, exc)
            return False
        finally:
            self._deactivate()
        return True

    @property
    def start_ns(self) -> int | None:
        return getattr(self._otel, "start_time", None)

    @property
    def record_id(self) -> RecordId:
        trace_id, span_id = _hex_ids(self._otel.get_span_context())
        return RecordId(trace_id=trace_id, span_id=span_id)

    def set_request_attributes(self, model: str | None, model_parameters: dict[str, Any] | None) -> None:
        if model is not None:
            self._otel.set_attribute(attr.GEN_AI_REQUEST_MODEL, model)
        if model_parameters is not None:
            self._otel.set_attribute(attr.MODEL_PARAMETERS, self._writer.encode_field(model_parameters, self.name))

    def stamp_attribution(self) -> None:
        self._otel.set_attributes(merged_attributes(self._writer.encode_field, self.name))

    def _deactivate(self) -> None:
        """Restore the previous current span when ``end()`` runs in the context that activated this one.

        Callback frameworks end a run in a different context than the one that started it
        (an async LangChain / LangGraph run fires its end callback in a fresh task). There
        the activating context cannot be restored from here: its activation stays set but
        is inert, since an ended handle is never current (``_live_current``), and it unwinds
        when an enclosing activation of that context ends.
        """
        handle_token, activation = self._handle_token, self._activation
        self._handle_token = self._activation = None
        if handle_token is None or activation is None:
            return
        try:
            _ACTIVE_HANDLE.reset(handle_token)
        except ValueError:
            return
        otel_context.detach(activation)


_ACTIVE_HANDLE: ContextVar[_OtelSpan | None] = ContextVar("tai42_otel_active_handle", default=None)


def _live_current() -> tuple[_OtelSpan | None, otel_trace.Span]:
    """The innermost live activated handle (``None`` when there is none) and the current span.

    An ended handle is never current: it is skipped, together with the span it made current,
    in favour of what was current before it.
    """
    handle = _ACTIVE_HANDLE.get()
    if handle is None or not handle.ended:
        return handle, otel_trace.get_current_span()
    while handle is not None and handle.ended:
        previous_span = handle._previous_span
        handle = handle._previous_handle
    if handle is not None:
        return handle, handle._otel
    return None, previous_span


def _usage_attributes(usage: TokenUsage) -> dict[str, int | float]:
    out: dict[str, int | float] = {}
    if usage.input_tokens is not None:
        out[attr.GEN_AI_USAGE_INPUT_TOKENS] = usage.input_tokens
    if usage.output_tokens is not None:
        out[attr.GEN_AI_USAGE_OUTPUT_TOKENS] = usage.output_tokens
    if usage.total_tokens is not None:
        out[attr.USAGE_TOTAL_TOKENS] = usage.total_tokens
    if usage.cost_usd is not None:
        out[attr.USAGE_COST_USD] = usage.cost_usd
    return out


def _apply_level(otel_span: otel_trace.Span, level: MonitoringLevel | None, status_message: str | None) -> None:
    if level is not None and level is not MonitoringLevel.DEFAULT:
        otel_span.set_attribute(attr.LEVEL, MonitoringLevel(level).value)
        if level is MonitoringLevel.ERROR:
            otel_span.set_status(Status(StatusCode.ERROR, status_message))
    if status_message is not None:
        otel_span.set_attribute(attr.STATUS_MESSAGE, status_message)


class OtelWriter:
    """A ``MonitoringWriter`` over the OpenTelemetry SDK, exporting OTLP/HTTP to a collector.

    ``resource_attributes`` are merged over the SDK's environment-detected resource (explicit
    wins). ``exporter`` replaces the OTLP exporter (tests pass an in-memory one); ``None``
    builds ``OTLPSpanExporter()``, which reads the standard ``OTEL_EXPORTER_OTLP_*``
    environment itself.

    Raises ``RuntimeError`` at construction when the processor's queue-full drops could not
    be counted (``OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED`` is not ``true``) or, with no
    ``exporter``, when no OTLP endpoint is configured.
    """

    def __init__(
        self,
        *,
        resource_attributes: Mapping[str, str] | None = None,
        exporter: SpanExporter | None = None,
    ) -> None:
        """Validate the environment; the pipeline is built lazily on first use."""
        if os.environ.get(_INTERNAL_METRICS_ENV, "").strip().lower() != "true":
            raise RuntimeError(
                f"monitoring requires {_INTERNAL_METRICS_ENV}=true: without it the span processor's queue-full "
                "drops are not counted"
            )
        if exporter is None and not any(os.environ.get(name) for name in _ENDPOINT_ENVS):
            raise RuntimeError(
                "monitoring requires OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (or OTEL_EXPORTER_OTLP_ENDPOINT): the "
                "OTLP/HTTP endpoint of the collector"
            )
        self._resource_attributes = dict(resource_attributes or {})
        self._exporter = exporter
        self._health = ExportHealthCounters()
        self._build_lock = threading.Lock()
        self._pipeline: _Pipeline | None = None
        self._inherited: list[_Pipeline] = []
        self._listener: HealthListener | None = None
        self._pid = os.getpid()
        weak_reset = weakref.WeakMethod(self._reset_after_fork)

        def _after_fork_in_child() -> None:
            reset = weak_reset()
            if reset is not None:
                reset()

        os.register_at_fork(after_in_child=_after_fork_in_child)

    # --- process / pipeline -------------------------------------------------

    def _check_pid(self) -> None:
        if os.getpid() != self._pid:
            self._reset_after_fork()

    def _reset_after_fork(self) -> None:
        if os.getpid() == self._pid:
            return
        self._health = ExportHealthCounters()
        self._build_lock = threading.Lock()
        if self._pipeline is not None:
            self._inherited.append(self._pipeline)
        self._pipeline = None
        self._pid = os.getpid()

    def _ensure_built(self) -> _Pipeline:
        pipeline = self._pipeline
        if pipeline is not None:
            return pipeline
        with self._build_lock:
            if self._pipeline is None:
                self._pipeline = self._build()
            return self._pipeline

    def _build(self) -> _Pipeline:
        resource = Resource.create(dict(self._resource_attributes))
        meter_provider = MeterProvider(
            metric_readers=[PeriodicExportingMetricReader(QueueDropMetricExporter(self._health))],
            resource=resource,
        )
        provider = TracerProvider(
            resource=resource,
            id_generator=ChosenTraceIdGenerator(),
            span_limits=SpanLimits(
                max_span_attributes=_MAX_SPAN_ATTRIBUTES,
                max_attribute_length=SpanLimits.UNSET,
                max_span_attribute_length=SpanLimits.UNSET,
            ),
            shutdown_on_exit=False,
            meter_provider=meter_provider,
        )
        provider.add_span_processor(AttributionSpanProcessor(self.encode_field))
        exporter = self._exporter if self._exporter is not None else OTLPSpanExporter()
        provider.add_span_processor(
            BatchSpanProcessor(CountingSpanExporter(exporter, self._health), meter_provider=meter_provider)
        )
        tracer = provider.get_tracer(TRACER_NAME)
        assert isinstance(tracer, Tracer)  # noqa: S101 - the SDK provider returns its own tracer
        return _Pipeline(provider=provider, meter_provider=meter_provider, tracer=tracer)

    # --- encoding and failure accounting --------------------------------------

    def record_failure(self, operation: str, record_name: str | None, exc: BaseException) -> None:
        """Log and count a record (or one field of it) this writer could not build."""
        logger.error("monitoring %s failed for record %r", operation, record_name, exc_info=exc)
        self._health.add_record_failed(f"{operation} failed for record {record_name!r}: {exc!r}")

    def encode_field(self, value: Any, record_name: str) -> str:
        """``value`` encoded once; an unencodable value becomes the writer's ``$tai42_unencodable`` marker.

        Never raises: the failure is logged at ERROR and counted.
        """
        try:
            return encode_payload(value)
        except Exception as exc:
            type_name = type(value).__qualname__
            logger.error(  # noqa: TRY400 - an encode refusal names its cause; no traceback adds to it
                "monitoring could not encode a %s value for record %r: %s", type_name, record_name, exc
            )
            self._health.add_record_failed(f"cannot encode a {type_name} value for record {record_name!r}: {exc}")
            return orjson.dumps({UNENCODABLE_KEY: type_name}).decode()

    def metadata_attributes(self, metadata: Mapping[str, Any], record_name: str) -> dict[str, str]:
        """Promoted keys as their own string attributes, every other key inside one ``tai42.metadata`` JSON object."""
        out: dict[str, str] = {}
        rest: dict[str, Any] = {}
        for key, value in metadata.items():
            if key in PROMOTED_METADATA_KEYS:
                out[key] = str(value) if isinstance(value, str) else self.encode_field(value, record_name)
            else:
                rest[key] = value
        if rest:
            out[attr.METADATA] = self.encode_field(rest, record_name)
        return out

    # --- span construction ----------------------------------------------------

    def _parent_context(self, trace_context: TraceContext | None) -> tuple[Context | None, int | None]:
        """The context a span starts in, and the trace id a parent-less span must take."""
        if trace_context is None or not trace_context.trace_id:
            _, current = _live_current()
            return otel_trace.set_span_in_context(current), None
        trace_id = _parse_hex(trace_context.trace_id, 32)
        if trace_context.parent_span_id:
            parent = SpanContext(
                trace_id,
                _parse_hex(trace_context.parent_span_id, 16),
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            return otel_trace.set_span_in_context(NonRecordingSpan(parent), Context()), None
        return Context(), trace_id

    def _start(
        self,
        *,
        name: str,
        kind: SpanKind,
        trace_context: TraceContext | None,
        start_time: datetime | None,
    ) -> _OtelSpan | None:
        """Start a span, or ``None`` when the trace context is invalid (logged and counted)."""
        try:
            parent, chosen = self._parent_context(trace_context)
        except ValueError as exc:
            logger.error("monitoring: invalid trace context %r for record %r", trace_context, name)  # noqa: TRY400
            self._health.add_record_failed(f"invalid trace context for record {name!r}: {exc}")
            return None
        tracer = self._ensure_built().tracer
        attributes = attr.kind_attributes(kind, name)
        if chosen is None:
            otel_span = tracer.start_span(name, context=parent, attributes=attributes, start_time=_ns(start_time))
        else:
            with chosen_trace_id(chosen):
                otel_span = tracer.start_span(name, context=parent, attributes=attributes, start_time=_ns(start_time))
        return _OtelSpan(self, otel_span, kind)

    # --- emit (fail-safe) -----------------------------------------------------

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
        """Open a span and return its handle; ``end()`` records it (fail-safe)."""
        self._check_pid()
        if _DISABLED.get():
            return _NOOP_SPAN
        try:
            span = self._start(name=name, kind=kind, trace_context=trace_context, start_time=start_time)
            if span is None:
                return _NOOP_SPAN
            if input_ is not None:
                span.set_input(input_)
            span.set_request_attributes(model, model_parameters)
            if metadata:
                span.merge_metadata(metadata)
            if activate:
                span.activate()
        except Exception as exc:
            self.record_failure("open_span", name, exc)
            return _NOOP_SPAN
        return span

    @contextmanager
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
    ) -> Iterator[Span]:
        """Open an activated span for the block and end it on exit (fail-safe)."""
        span = self.open_span(
            name=name,
            kind=kind,
            trace_context=trace_context,
            input_=input_,
            model=model,
            model_parameters=model_parameters,
            metadata=metadata,
            activate=True,
        )
        try:
            yield span
        finally:
            span.end()

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
        """Record an already-closed span with explicit times; a missing ``trace_id`` raises (a caller bug)."""
        if not trace_context.trace_id:
            raise ValueError(
                "record_span requires trace_context.trace_id: the explicit-time path has no ambient context to "
                "attach to"
            )
        span = self.open_span(
            name=name,
            kind=kind,
            trace_context=trace_context,
            input_=input_,
            model=model,
            metadata=metadata,
            start_time=start,
        )
        span.update(output=output, usage=usage, level=level, status_message=status_message)
        span.end(end_time=end)

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
        """Record a point-in-time event (a span started and ended at one instant); its id, or ``None``."""
        span = self.open_span(
            name=name, kind=SpanKind.EVENT, trace_context=trace_context, input_=input_, metadata=metadata
        )
        if not isinstance(span, _OtelSpan):
            return None
        span.update(output=output, level=level, status_message=status_message)
        return span.record_id if span.finish(span.start_ns) else None

    def update_current_span(
        self,
        *,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        input_: Any = None,
        output: Any = None,
    ) -> None:
        """Amend the current span (the innermost activated one) (fail-safe)."""
        self._check_pid()
        if _DISABLED.get():
            return
        handle, _ = _live_current()
        if handle is None:
            return
        try:
            if input_ is not None:
                handle.set_input(input_)
        except Exception as exc:
            self.record_failure("update_current_span", handle.name, exc)
        handle.update(output=output, metadata=metadata, level=level, status_message=status_message)

    @contextmanager
    def trace_attributes(
        self,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[None]:
        """Stamp trace-level attributes on the current span and on every span started in the block."""
        frame = AttributionFrame(
            name=name,
            tags=tuple(tags or ()),
            metadata=dict(metadata or {}),
            user_id=user_id,
            session_id=session_id,
        )
        with pushed_frame(frame):
            handle, _ = _live_current()
            if handle is not None and not _DISABLED.get():
                try:
                    handle.stamp_attribution()
                except Exception as exc:
                    self.record_failure("trace_attributes", handle.name, exc)
            yield

    # --- queries (never raise) ------------------------------------------------

    def current_trace_id(self) -> str | None:
        """The current span's trace id (32 lower-hex), or ``None``."""
        span_context = _live_current()[1].get_span_context()
        return _hex_ids(span_context)[0] if span_context.is_valid else None

    def current_span_id(self) -> str | None:
        """The current span's id (16 lower-hex), or ``None``."""
        span_context = _live_current()[1].get_span_context()
        return _hex_ids(span_context)[1] if span_context.is_valid else None

    def is_recording(self) -> bool:
        """This writer records."""
        return True

    def export_health(self) -> MonitoringExportHealth:
        """The counts of this process's undelivered records."""
        self._check_pid()
        return self._health.snapshot()

    def set_health_listener(self, listener: HealthListener | None) -> None:
        """Install (``None`` removes) the listener called after every ``flush()`` / ``shutdown()``."""
        self._listener = listener

    # --- suppression / lifecycle ----------------------------------------------

    @contextmanager
    def disable(self) -> Iterator[None]:
        """Record nothing in the block."""
        token = _DISABLED.set(True)
        try:
            yield
        finally:
            _DISABLED.reset(token)

    def flush(self) -> None:
        """Export (or count as failed) every ended span, collect the queue-drop count, then call the listener."""
        self._check_pid()
        pipeline = self._pipeline
        if pipeline is not None:
            if not pipeline.provider.force_flush():
                logger.error("monitoring flush did not complete within the span processor's timeout")
            if not pipeline.meter_provider.force_flush():
                logger.error("monitoring flush did not collect the queue-drop count within its timeout")
        self._notify_listener()

    def shutdown(self) -> None:
        """Shut the pipeline down (the next use rebuilds it), then call the listener."""
        self._check_pid()
        with self._build_lock:
            pipeline, self._pipeline = self._pipeline, None
        if pipeline is not None:
            pipeline.provider.shutdown()
            pipeline.meter_provider.shutdown()
        self._notify_listener()

    def _notify_listener(self) -> None:
        listener = self._listener
        if listener is None:
            return
        try:
            listener(self._health.snapshot())
        except Exception:
            logger.exception("monitoring health listener failed")
