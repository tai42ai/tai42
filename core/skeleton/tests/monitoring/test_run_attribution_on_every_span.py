"""Every span the platform writes for a run carries the run's trace name, tags, user and session.

Driven through the real app and the kit's OpenTelemetry writer over an in-memory exporter: the
runs chokepoint's ``run`` span and the deferred call's ``deferred-call:<tool>`` span.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import orjson
import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tai42_contract.agent.base import PresetSpec
from tai42_contract.monitoring import RUN_ATTRIBUTION_TRACE_NAME, RunAttribution
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.monitoring.otel import OtelWriter
from tai42_kit.monitoring.otel import attributes as attr

import tai42_skeleton.operations._authority as authority_module
import tai42_skeleton.runs.chokepoint as chokepoint
import tai42_skeleton.tools.tier as tier_module
import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.monitoring import NoOpMonitoring, NoOpReader, init_monitoring, reset_monitoring
from tai42_skeleton.operations.errors import ForbiddenError
from tai42_skeleton.tools.attribution import run_attribution
from tai42_skeleton.tools.binding.surface import remove_surface_tool
from tai42_skeleton.tools.deferred import ToolCallKind

from ..versioning.conftest import FakeVersioningPg

_ATTRIBUTION = RunAttribution(tags=["alpha", "beta"], user_id="alice", session_id="thread-1")
_TRACE = "a" * 32
_PARENT = "b" * 16
_MANIFEST = Manifest.model_validate(
    {
        "tools": [
            {
                "title": "fx",
                "module": "tests.monitoring._attribution_fixtures",
                "include": ["attributed_leaf", "attributed_boom"],
            }
        ]
    }
)


class _OtelMonitoring(NoOpMonitoring):
    def __init__(self, writer: OtelWriter) -> None:
        super().__init__()
        self._otel_writer = writer

    @property
    def writer(self) -> OtelWriter:
        return self._otel_writer

    @property
    def reader(self) -> NoOpReader:
        return self._reader


class _Store:
    async def insert_start(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def update_outcome(self, *_args: object, **_kwargs: object) -> None:
        return None


@pytest.fixture
def pg(monkeypatch: pytest.MonkeyPatch) -> FakeVersioningPg:
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "test")
    fake = FakeVersioningPg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        assert client_cls is PostgresClient
        yield fake

    monkeypatch.setattr(store_module, "client_ctx", fake_client_ctx)
    return fake


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    monkeypatch.setattr(chokepoint, "component_store_configured", lambda _c: True)
    monkeypatch.setattr(chokepoint, "get_run_index_store", lambda: _Store())
    memory = InMemorySpanExporter()
    writer = OtelWriter(exporter=memory)
    reset_monitoring()
    init_monitoring(_OtelMonitoring(writer))
    yield memory
    writer.shutdown()
    reset_monitoring()


@pytest.fixture(autouse=True)
def _clean_server() -> Iterator[None]:
    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            remove_surface_tool(app._fast_mcp, tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


def _finished(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {span.name: span for span in exporter.get_finished_spans()}


def _assert_attributed(span: ReadableSpan) -> None:
    attributes = dict(span.attributes or {})
    assert attributes.get(attr.TRACE_NAME) == RUN_ATTRIBUTION_TRACE_NAME, (span.name, attributes)
    tags = orjson.loads(str(attributes.get(attr.TRACE_TAGS, "[]")))
    assert {"alpha", "beta"} <= set(tags), (span.name, attributes)
    assert attributes.get(attr.USER_ID) == "alice", (span.name, attributes)
    assert attributes.get(attr.SESSION_ID) == "thread-1", (span.name, attributes)


async def _register_preset(name: str, base_tool: str, fixed_kwargs: dict) -> None:
    await app.presets.store.create_preset(
        PresetSpec(name=name, description="d", base_tool=base_tool, fixed_kwargs=fixed_kwargs), extensions=[]
    )
    body = await app.presets.store.get_active_body(name)
    await app.preset_manager.register(name, body.base_tool, body.fixed_kwargs, body.extensions, body.description)


def _payload(tool: str, arguments: dict) -> dict:
    return {
        "tool": tool,
        "arguments": arguments,
        "attribution": _ATTRIBUTION.model_dump(mode="json"),
        "state_context": None,
        "identity": None,
        "trace_id": _TRACE,
        "parent_span_id": _PARENT,
    }


def test_the_run_span_of_a_preset_run_carries_the_run_attribution(
    pg: FakeVersioningPg, exporter: InMemorySpanExporter
) -> None:
    async def run() -> None:
        async with app.app_context(_MANIFEST):
            await _register_preset("attributed-run", "attributed_leaf", {"city": "paris"})
            with run_attribution(_ATTRIBUTION):
                assert await app.tools.run_tool("attributed-run", {}) == {"city": "paris"}

    asyncio.run(run())
    run_span = _finished(exporter)[RUN_ATTRIBUTION_TRACE_NAME]
    _assert_attributed(run_span)
    assert "preset:attributed-run" in orjson.loads(str((run_span.attributes or {})[attr.TRACE_TAGS]))


def test_a_deferred_call_span_carries_the_run_attribution(exporter: InMemorySpanExporter) -> None:
    async def run() -> None:
        async with app.app_context(_MANIFEST):
            await ToolCallKind().apply(_payload("attributed_leaf", {"city": "paris"}), idempotency_key="k-1")

    asyncio.run(run())
    _assert_attributed(_finished(exporter)["deferred-call:attributed_leaf"])


def test_a_deferred_call_whose_tool_raises_still_carries_the_run_attribution(
    exporter: InMemorySpanExporter,
) -> None:
    async def run() -> None:
        async with app.app_context(_MANIFEST):
            with pytest.raises(RuntimeError, match="boom"):
                await ToolCallKind().apply(_payload("attributed_boom", {}), idempotency_key="k-2")

    asyncio.run(run())
    _assert_attributed(_finished(exporter)["deferred-call:attributed_boom"])


def test_a_deferred_call_refused_before_its_dispatch_still_carries_the_run_attribution(
    exporter: InMemorySpanExporter,
) -> None:
    async def run() -> None:
        async with app.app_context(_MANIFEST):
            with pytest.raises(Exception, match="no-such-tool"):
                await ToolCallKind().apply(_payload("no-such-tool", {}), idempotency_key="k-3")

    asyncio.run(run())
    _assert_attributed(_finished(exporter)["deferred-call:no-such-tool"])


def test_a_deferred_call_refused_by_the_run_tier_fence_still_carries_the_run_attribution(
    exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _non_admin() -> SimpleNamespace:
        return SimpleNamespace(is_admin=False)

    monkeypatch.setattr(tier_module, "resolve_run_tier", lambda _app, _key: "fenced")
    monkeypatch.setattr(authority_module, "resolve_caller", _non_admin)

    async def run() -> None:
        async with app.app_context(_MANIFEST):
            with pytest.raises(ForbiddenError):
                await ToolCallKind().apply(_payload("attributed_leaf", {"city": "paris"}), idempotency_key="k-4")

    asyncio.run(run())
    _assert_attributed(_finished(exporter)["deferred-call:attributed_leaf"])
