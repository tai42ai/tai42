"""The ``monitor`` branch records the caller-supplied arguments as its span input.

A branch over a synthesized signature hands the wrapper every optional parameter the caller left
out as the omitted-value marker; the span input holds only what the caller supplied, so the real
OpenTelemetry writer encodes it cleanly and counts no record failure.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import orjson
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tai42_kit.monitoring.otel import OtelWriter

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.monitoring import init_monitoring, reset_monitoring

from .._fakes.recording_monitoring import RecordingMonitoring


@pytest.fixture(autouse=True)
def _reset_monitoring() -> Iterator[None]:
    reset_monitoring()
    yield
    reset_monitoring()


@pytest.fixture(autouse=True)
def _clean_server() -> Iterator[None]:
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


def test_an_agent_run_tools_monitor_branch_records_only_the_supplied_arguments(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    exporter = InMemorySpanExporter()
    writer = OtelWriter(exporter=exporter)
    backend = RecordingMonitoring()
    backend._recording_writer = writer  # type: ignore[assignment]
    init_monitoring(backend)
    manifest = Manifest.model_validate(
        {
            "extensions_modules": ["tai42_skeleton.extensions.builtin.monitor"],
            "tools": [
                {
                    "title": "fxt",
                    "module": "tests.app._fixtures.tools_b",
                    "include": ["shout"],
                    "extensions": {"echo_fields": [["monitor"]]},
                }
            ],
            "agents": [{"title": "agents", "module": "tests.agent._fixtures", "include": ["echo_fields"]}],
        }
    )

    async def run() -> str:
        async with app.app_context(manifest):
            return await app.tools.run_tool("echo_fields_monitor", {"text": "hi"})

    try:
        assert asyncio.run(run()) == "text"
        writer.flush()
        (record,) = exporter.get_finished_spans()
        arguments = orjson.loads(str(dict(record.attributes or {})["gen_ai.tool.call.arguments"]))
        assert arguments == {"args": [], "kwargs": {"text": "hi"}}
        assert writer.export_health().records_failed == 0
    finally:
        writer.shutdown()
