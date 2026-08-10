"""A fixture monitoring backend that records span opens to the probe channel.

The ``monitor`` builtin tool extension traces a standalone tool call as one live
``SpanKind.TOOL`` span through ``get_monitoring().writer.start_span``. With the
default no-op backend that span goes nowhere observable, so this fixture backend
makes the extension's effect visible OFFLINE: its writer RPUSHes a record for
every span it opens onto the harness probe channel (Redis list
``e2e:rec:monitor_spans``), which a spec reads back through ``stack.records``.

It reuses the no-op backend for everything except ``start_span`` (reader, events,
trace context — none of which the extension exercises), so it stays a thin,
honest recorder rather than a second full monitoring implementation. A manifest
activates it by naming this module in ``monitoring_module``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import redis
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import Span, SpanKind, TraceContext
from tai42_skeleton.monitoring.noop import NoOpMonitoring, NoOpSpan, NoOpWriter

# The probe list the harness reads span records off (logical DB 0), mirroring the
# ``e2e:rec:{key}`` shape the ``e2e_record`` tool writes.
_SPAN_RECORD_KEY = "e2e:rec:monitor_spans"


class _RecordingWriter(NoOpWriter):
    """A no-op writer that additionally records each opened span to the probe
    channel — the one seam the ``monitor`` extension drives."""

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
    ) -> Iterator[Span]:
        record = json.dumps({"name": name, "kind": kind.value, "input": repr(input)})
        client = redis.Redis.from_url(os.environ["E2E_PROBE_REDIS_URL"])
        try:
            client.rpush(_SPAN_RECORD_KEY, record)
        finally:
            client.close()
        yield NoOpSpan()


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
