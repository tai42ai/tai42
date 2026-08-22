"""A fixture monitoring backend that records span opens, run attribution, and span cost.

The ``monitor`` builtin tool extension traces a standalone tool call as one live
``SpanKind.TOOL`` span through ``get_monitoring().writer.start_span``. The shared run
chokepoint (``ToolBinding.run_tool``) wraps a run in ``attribute_run`` (over
``trace_attributes``) so the run's spans open INSIDE the ambient ``RunAttribution`` scope,
and the ``claude_code`` adapter emits SDK-reported model cost via ``start_span`` +
``Span.update(usage_details=...)`` (its model calls bypass the platform LLM seam). With the
default no-op backend all three go nowhere observable, so this fixture backend makes them
visible OFFLINE by RPUSHing a record onto a harness probe channel a spec reads back through
``stack.records``:

* ``e2e:rec:monitor_spans`` — one record per opened span, STAMPED with the active run
  attribution (tags/metadata) when a ``trace_attributes`` block is entered, so a spec proves
  the span fell INSIDE the attribution scope (not merely that the scope was entered).
* ``e2e:rec:trace_attrs`` — one record per entered ``trace_attributes`` block.
* ``e2e:rec:span_cost`` — one record per ``Span.update(usage_details=...)`` cost emission.

``current_trace_id`` reports an active trace while a span is open OR when the stack opts in
via ``E2E_MONITOR_ACTIVE_TRACE`` — the attribution wrap and the cost emission are both guarded
by it, so a stack that drives them names the knob, while the standalone ``monitor`` extension
(which SUPPRESSES itself when a trace is already active) is left untouched on stacks that do
not. It stays a thin honest recorder — the no-op backend everywhere except these three seams,
never a second full monitoring implementation. A manifest activates it via ``monitoring_module``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import redis
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import MonitoringLevel, SpanKind, TraceContext
from tai42_skeleton.monitoring.noop import NoOpMonitoring, NoOpSpan, NoOpWriter

# The probe lists the harness reads records off (logical DB 0), mirroring the ``e2e:rec:{key}``
# shape the ``e2e_record`` tool writes.
_SPAN_RECORD_KEY = "e2e:rec:monitor_spans"
_TRACE_ATTRS_KEY = "e2e:rec:trace_attrs"
_SPAN_COST_KEY = "e2e:rec:span_cost"

# The constant id ``current_trace_id`` reports for an active recording trace — a fixture
# recorder needs only presence/absence, never a real OTel id.
_RECORDING_TRACE_ID = "e2e-rec-trace"

# The opt-in that makes ``current_trace_id`` report an active trace unconditionally, so the
# attribution wrap and the cost emission (both guarded by it) fire on a stack that drives them.
_ACTIVE_TRACE_ENV = "E2E_MONITOR_ACTIVE_TRACE"

# The active run attribution for the current task, set while a ``trace_attributes`` block is
# entered; ``start_span`` stamps it onto each span record opened inside the block. A ContextVar
# (not a plain stack) so concurrent runs never cross-attribute.
_active_attribution: ContextVar[dict[str, Any] | None] = ContextVar("tai42_e2e_active_attribution", default=None)
# The open-span depth for the current task — an open span IS an active trace.
_span_depth: ContextVar[int] = ContextVar("tai42_e2e_span_depth", default=0)


def _push(key: str, record: dict[str, Any]) -> None:
    client = redis.Redis.from_url(os.environ["E2E_PROBE_REDIS_URL"])
    try:
        client.rpush(key, json.dumps(record))
    finally:
        client.close()


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


class _RecordingSpan(NoOpSpan):
    """A span handle that records its cost emission — the one seam a generation span drives."""

    def __init__(self, *, name: str, kind: SpanKind) -> None:
        self._name = name
        self._kind = kind

    def update(
        self,
        *,
        output: Any = None,
        model: str | None = None,
        usage_details: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        level: MonitoringLevel | None = None,
        status_message: str | None = None,
    ) -> None:
        if usage_details is not None:
            _push(_SPAN_COST_KEY, {"name": self._name, "kind": self._kind.value, "usage_details": usage_details})


class _RecordingWriter(NoOpWriter):
    """A no-op writer that additionally records span opens, trace attribution, and span cost."""

    @contextmanager
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
    ) -> Iterator[NoOpSpan]:
        record = {
            "name": name,
            "kind": kind.value,
            "input": repr(input),
            # The active run attribution when this span opened, or ``None`` — so a spec proves
            # the span fell INSIDE the attribution scope, not merely that the scope was entered.
            "attribution": _active_attribution.get(),
        }
        _push(_SPAN_RECORD_KEY, record)
        token = _span_depth.set(_span_depth.get() + 1)
        try:
            yield _RecordingSpan(name=name, kind=kind)
        finally:
            _span_depth.reset(token)

    @contextmanager
    def trace_attributes(
        self,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        attribution = {"name": name, "tags": list(tags or []), "metadata": dict(metadata or {})}
        _push(_TRACE_ATTRS_KEY, attribution)
        token = _active_attribution.set(attribution)
        try:
            yield
        finally:
            _active_attribution.reset(token)

    def current_trace_id(self) -> str | None:
        if _span_depth.get() > 0 or _truthy(os.environ.get(_ACTIVE_TRACE_ENV)):
            return _RECORDING_TRACE_ID
        return None


class _RecordingMonitoring(NoOpMonitoring):
    def __init__(self) -> None:
        super().__init__()
        self._writer = _RecordingWriter()

    @property
    def writer(self) -> _RecordingWriter:
        return self._writer


@tai42_app.monitoring.register_monitoring
def build_recording_monitoring() -> _RecordingMonitoring:
    return _RecordingMonitoring()
