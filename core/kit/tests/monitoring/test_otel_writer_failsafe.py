"""``OtelWriter`` fail-safety and fork reset: a failing record is logged and counted, never raised."""

from __future__ import annotations

import logging
import os
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tai42_contract.monitoring import MonitoringExportHealth, SpanKind

from tai42_kit.monitoring.otel import OtelWriter
from tai42_kit.monitoring.otel import writer as writer_module


def _raise(*_: Any, **__: Any) -> None:
    raise RuntimeError("sdk broke")


@pytest.mark.parametrize("method", ["update", "set_trace_metadata", "end"])
def test_a_failing_span_method_is_logged_and_counted(writer: OtelWriter, caplog: pytest.LogCaptureFixture, method: str):
    span = writer.open_span(name="n", kind=SpanKind.CHAIN)
    otel = span._otel  # type: ignore[attr-defined]
    otel.set_attribute = _raise
    otel.set_attributes = _raise
    with caplog.at_level(logging.ERROR):
        if method == "update":
            span.update(output={"a": 1})
        elif method == "set_trace_metadata":
            span.set_trace_metadata(name="x")
        else:
            span.end()
    assert writer.export_health().records_failed == 1
    assert "sdk broke" in (writer.export_health().last_error or "")
    assert "failed for record 'n'" in caplog.text


def test_a_failing_open_is_counted_and_returns_an_inert_handle(writer: OtelWriter, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(writer, "_start", _raise)
    span = writer.open_span(name="n", kind=SpanKind.CHAIN)
    assert span.id == ""
    span.update(output=1)
    span.set_trace_metadata(name="n", tags=["t"])
    span.end()
    assert writer.export_health().records_failed == 1


def test_a_failing_create_event_returns_none(writer: OtelWriter, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(writer, "_start", _raise)
    assert writer.create_event(name="e") is None


def test_a_failing_current_input_is_counted(writer: OtelWriter, monkeypatch: pytest.MonkeyPatch):
    with writer.start_span(name="n", kind=SpanKind.CHAIN):
        monkeypatch.setattr(writer_module._OtelSpan, "set_input", _raise)
        writer.update_current_span(input_={"x": 1}, output={"y": 2})
    assert writer.export_health().records_failed == 1


def test_a_failing_attribution_stamp_is_counted(writer: OtelWriter, monkeypatch: pytest.MonkeyPatch):
    with writer.start_span(name="n", kind=SpanKind.CHAIN):
        monkeypatch.setattr(writer_module._OtelSpan, "stamp_attribution", _raise)
        with writer.trace_attributes(name="t"):
            pass
    assert writer.export_health().records_failed == 1


def test_a_flush_that_times_out_is_logged(writer: OtelWriter, caplog: pytest.LogCaptureFixture):
    writer.open_span(name="n", kind=SpanKind.CHAIN).end()
    pipeline = writer._pipeline
    assert pipeline is not None
    pipeline.provider.force_flush = lambda *a, **k: False  # type: ignore[method-assign]
    pipeline.meter_provider.force_flush = lambda *a, **k: False  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        writer.flush()
    assert "did not complete within the span processor's timeout" in caplog.text
    assert "did not collect the queue-drop count" in caplog.text


def test_a_process_change_resets_the_counts_and_builds_a_new_pipeline(
    writer: OtelWriter, exporter: InMemorySpanExporter
):
    writer.open_span(name="before", kind=SpanKind.CHAIN).end()
    writer._health.add_record_failed("in the parent")
    inherited = writer._pipeline
    writer._pid = -1  # as seen from a forked child
    assert writer.export_health() == MonitoringExportHealth()
    assert writer._pid == os.getpid()
    assert writer._inherited == [inherited]
    assert writer._pipeline is None
    writer.open_span(name="after", kind=SpanKind.CHAIN).end()
    assert writer._pipeline is not inherited
    writer.flush()
    assert "after" in [s.name for s in exporter.get_finished_spans()]


def test_the_at_fork_hook_resets_through_a_weak_reference(writer: OtelWriter):
    writer._pid = -1
    writer._reset_after_fork()
    assert writer._pid == os.getpid()
    writer._reset_after_fork()  # same process: nothing to reset
    assert writer._pid == os.getpid()
