"""The span handles ``OtelWriter`` returns, and the per-context stack of activations that says which span is current.

An :class:`OtelSpan` is one open record: its metadata is merged on the handle and written once, at ``end()``.
A :class:`NoOpSpan` stands for a record that is not written. Each activation (a span made current by
``activate()``, ``OtelWriter.use_span`` or ``OtelWriter.use_trace_context``) is an :class:`Activation` on its
context's :class:`ActivationStack`, held on a ``ContextVar``. Spans end in any order: :func:`detach` takes a span's
activation off wherever it sits. Leaving a block takes off its activation and every one made inside it
(:func:`unwind`), so nothing a block made current outlives it. :func:`live_current` reads the innermost live
activation of the calling context.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, NamedTuple

from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.context import Context
from opentelemetry.trace import SpanContext, Status, StatusCode
from tai42_contract.monitoring import MonitoringLevel, RecordId, SpanKind, TokenUsage

from tai42_kit.monitoring.otel import attributes as attr
from tai42_kit.monitoring.otel.attribution import merged_attributes

if TYPE_CHECKING:
    from tai42_kit.monitoring.otel.writer import OtelWriter


def to_ns(ts: datetime | None) -> int | None:
    return None if ts is None else int(ts.timestamp() * 1_000_000_000)


def hex_ids(span_context: SpanContext) -> tuple[str, str]:
    return format(span_context.trace_id, "032x"), format(span_context.span_id, "016x")


class NoOpSpan:
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


NOOP_SPAN = NoOpSpan()


class OtelSpan:
    """The handle of one open span. Metadata is merged here and written once, at ``end()``."""

    __slots__ = ("_activation", "_ended", "_kind", "_metadata", "_otel", "_writer")

    def __init__(self, writer: OtelWriter, otel_span: otel_trace.Span, kind: SpanKind) -> None:
        self._writer = writer
        self._otel = otel_span
        self._kind = kind
        self._metadata: dict[str, Any] = {}
        self._activation: Activation | None = None
        self._ended = False

    @property
    def id(self) -> str:
        return format(self._otel.get_span_context().span_id, "016x")

    @property
    def writer(self) -> OtelWriter:
        return self._writer

    @property
    def otel_span(self) -> otel_trace.Span:
        return self._otel

    @property
    def name(self) -> str:
        return getattr(self._otel, "name", "")

    @property
    def ended(self) -> bool:
        return self._ended

    def activate(self) -> None:
        self._activation = attach(self._otel, self)

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
        self.finish(to_ns(end_time))

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
        trace_id, span_id = hex_ids(self._otel.get_span_context())
        return RecordId(trace_id=trace_id, span_id=span_id)

    def set_request_attributes(self, model: str | None, model_parameters: dict[str, Any] | None) -> None:
        if model is not None:
            self._otel.set_attribute(attr.GEN_AI_REQUEST_MODEL, model)
        if model_parameters is not None:
            self._otel.set_attribute(attr.MODEL_PARAMETERS, self._writer.encode_field(model_parameters, self.name))

    def stamp_attribution(self) -> None:
        self._otel.set_attributes(merged_attributes(self._writer.encode_field, self.name))

    def _deactivate(self) -> None:
        """Take this span's activation off the calling context's stack, if that context holds it.

        Callback frameworks end a run in a different context than the one that started it
        (an async LangChain / LangGraph run fires its end callback in a fresh task). A context
        that never held the activation is left unchanged; one that holds it but sees the end
        from elsewhere keeps an inert activation, since an ended handle is never current
        (``live_current``).
        """
        activation, self._activation = self._activation, None
        if activation is not None:
            detach(activation)


@dataclass(frozen=True, slots=True, eq=False)
class Activation:
    """One span made current in a context: the span, its handle, and the OTel context it attached.

    ``handle`` is the writer's handle of ``span``, or ``None`` for a lineage made current by
    :meth:`OtelWriter.use_trace_context`, which no ``end()`` closes. An activation is compared by
    identity: :func:`detach` takes off exactly the one :func:`attach` returned.
    """

    span: otel_trace.Span
    handle: OtelSpan | None
    context: Context

    @property
    def live(self) -> bool:
        return self.handle is None or not self.handle.ended


class ActivationStack(NamedTuple):
    """A context's activations, innermost last, and the OTel context current before the outermost of them."""

    base: Context
    frames: tuple[Activation, ...]

    def innermost_live(self) -> Activation | None:
        return next((frame for frame in reversed(self.frames) if frame.live), None)


_STACK: ContextVar[ActivationStack | None] = ContextVar("tai42_otel_activations", default=None)


def attach(span: otel_trace.Span, handle: OtelSpan | None) -> Activation:
    """Make ``span`` current in the calling context, as the innermost activation of its stack."""
    before = otel_context.get_current()
    activation = Activation(span=span, handle=handle, context=otel_trace.set_span_in_context(span, before))
    stack = _STACK.get()
    _STACK.set(
        ActivationStack(before, (activation,)) if stack is None else stack._replace(frames=(*stack.frames, activation))
    )
    otel_context.attach(activation.context)
    return activation


def detach(activation: Activation) -> None:
    """Take ``activation`` alone off the calling context's stack, wherever it sits (a span's ``end()``).

    The activations above it stay, and the innermost live one is current. A context whose stack does not hold
    ``activation`` (it was attached in another one, or a block it was made in has been left) is left unchanged.
    """
    stack = _STACK.get()
    if stack is not None and any(frame is activation for frame in stack.frames):
        _settle(stack.base, tuple(frame for frame in stack.frames if frame is not activation))


def unwind(activation: Activation) -> None:
    """Take ``activation`` and every activation made after it off the calling context's stack (leaving a block).

    What a block made current does not outlive the block: the stack is again what it was before the block's
    activation. A context whose stack does not hold ``activation`` is left unchanged.
    """
    stack = _STACK.get()
    if stack is None:
        return
    for index, frame in enumerate(stack.frames):
        if frame is activation:
            _settle(stack.base, stack.frames[:index])
            return


def _settle(base: Context, frames: tuple[Activation, ...]) -> None:
    """Hold ``frames`` as the calling context's stack and make its innermost live activation's context current."""
    stack = ActivationStack(base, frames)
    live = stack.innermost_live()
    _STACK.set(stack if frames else None)
    otel_context.attach(base if live is None else live.context)


def live_current() -> tuple[OtelSpan | None, otel_trace.Span]:
    """The handle of the innermost live activation (``None`` for a lineage or none) and the current span.

    An ended handle is never current: its activation is skipped in favour of the innermost live
    one; with no activation of the context live, the current span is the one current before the
    outermost of them.
    """
    stack = _STACK.get()
    if stack is None:
        return None, otel_trace.get_current_span()
    live = stack.innermost_live()
    if live is None:
        return None, otel_trace.get_current_span(stack.base)
    return live.handle, live.span


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
