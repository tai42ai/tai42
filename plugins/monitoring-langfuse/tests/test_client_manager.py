"""Client-manager coverage: the one read client, built once, with tracing turned off."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from opentelemetry import trace as otel_trace

from tai42_monitoring_langfuse.client_manager import LangfuseClientManager
from tai42_monitoring_langfuse.project import LangfuseProject


def _cfg(source: str = "tai", timeout_seconds: int = 30) -> LangfuseProject:
    return LangfuseProject(
        public_key="pk", secret_key="sk", host="http://localhost", source=source, timeout_seconds=timeout_seconds
    )


@pytest.fixture
def fake_langfuse(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace the SDK client constructor; returns the captured build kwargs."""
    constructed: list[dict] = []
    monkeypatch.setattr(
        "tai42_monitoring_langfuse.client_manager.Langfuse",
        lambda **kw: constructed.append(kw) or MagicMock(name="client"),
    )
    return constructed


def test_builds_one_read_client_with_tracing_off(fake_langfuse: list[dict]) -> None:
    mgr = LangfuseClientManager(_cfg(source="shared-team", timeout_seconds=7))
    client = mgr.active_client()
    assert mgr.active_client() is client
    assert fake_langfuse == [
        {
            "public_key": "pk",
            "secret_key": "sk",
            "base_url": "http://localhost",
            "timeout": 7,
            "environment": "shared-team",
            "tracing_enabled": False,
        }
    ]


def test_source_and_read_timeout_come_from_the_project() -> None:
    mgr = LangfuseClientManager(_cfg(source="staging", timeout_seconds=9))
    assert mgr.active_source() == "staging"
    assert mgr.read_timeout_seconds() == 9


def test_the_real_client_installs_no_global_tracer_provider() -> None:
    mgr = LangfuseClientManager(_cfg())
    client = mgr.active_client()
    try:
        assert type(otel_trace.get_tracer_provider()).__name__ == "ProxyTracerProvider"
    finally:
        client.shutdown()
