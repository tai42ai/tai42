"""Shared fixtures for the OpenTelemetry writer tests: an in-memory exporter behind the writer's own pipeline."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from typing import Any

import orjson
import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tai42_kit.monitoring.otel import OtelWriter

_OTEL_ENV = (
    "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_BSP_MAX_QUEUE_SIZE",
    "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
    "OTEL_BSP_SCHEDULE_DELAY",
    "OTEL_SERVICE_NAME",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT",
    "OTEL_SPAN_ATTRIBUTE_VALUE_LENGTH_LIMIT",
)


@pytest.fixture(autouse=True)
def otel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every writer test runs with the processor self-metrics on and no ambient OTel env."""
    for name in _OTEL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def writer(exporter: InMemorySpanExporter) -> Iterator[OtelWriter]:
    w = OtelWriter(exporter=exporter, resource_attributes={"deployment.environment.name": "test-env"})
    yield w
    w.shutdown()


def finished(writer: OtelWriter, exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    writer.flush()
    return list(exporter.get_finished_spans())


def only(writer: OtelWriter, exporter: InMemorySpanExporter) -> ReadableSpan:
    spans = finished(writer, exporter)
    assert len(spans) == 1, [s.name for s in spans]
    return spans[0]


def attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def decoded(span: ReadableSpan, key: str) -> Any:
    return orjson.loads(attrs(span)[key])


class BlockingExporter(SpanExporter):
    """Blocks inside ``export`` until released; signals when it was entered."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.exported: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.entered.set()
        self.release.wait(10)
        self.exported.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.release.set()


class ReusableExporter(InMemorySpanExporter):
    """An in-memory exporter that keeps accepting spans after a shutdown, counting the shutdowns."""

    def __init__(self) -> None:
        super().__init__()
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1
