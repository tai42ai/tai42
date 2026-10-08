"""Factory: the backend built from the ``LANGFUSE_*`` environment, loud failure without credentials."""

from __future__ import annotations

import pytest
from tai42_contract.monitoring import Monitoring
from tai42_kit.monitoring.otel import OtelWriter

from tai42_monitoring_langfuse import LangfuseMonitoring, build_langfuse_backend


@pytest.fixture
def otel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces")


def test_langfuse_without_creds_raises(monkeypatch: pytest.MonkeyPatch, otel_env: None) -> None:
    # Selecting Langfuse but leaving credentials unset is a misconfiguration:
    # it must fail loudly, not silently degrade to no-op.
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="LANGFUSE_PUBLIC_KEY"):
        build_langfuse_backend()


def test_langfuse_with_creds_builds_an_otel_writer_stamped_with_the_source(
    monkeypatch: pytest.MonkeyPatch, otel_env: None
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost")
    monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "staging")
    backend = build_langfuse_backend()
    assert isinstance(backend, LangfuseMonitoring)
    assert isinstance(backend, Monitoring)  # runtime-checkable contract protocol
    assert isinstance(backend.writer, OtelWriter)
    assert backend.writer._resource_attributes == {"deployment.environment.name": "staging"}
    assert backend._manager.active_source() == "staging"


def test_the_writer_refuses_without_an_otlp_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost")
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    with pytest.raises(RuntimeError, match="OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        build_langfuse_backend()
