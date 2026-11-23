"""The timing saver every saver the kit hands out is wrapped in: one span per store wait inside a live trace.

The inner saver is a fake recording each call; the spans go through the real ``OtelWriter`` to an in-memory
exporter and are read back as the observations the writer recorded.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("langgraph")

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    DeltaChannelHistory,
    empty_checkpoint,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import (
    STEP_ROLE_METADATA_KEY,
    MonitoringObservation,
    SpanKind,
    StepRole,
)

from tai42_kit.llm.checkpoint.timing import TimedSaver
from tai42_kit.monitoring.otel import OtelWriter
from tai42_kit.monitoring.testing import span_to_observation

PROVIDER = "probe"
CHECKPOINT = empty_checkpoint()


def _config(thread_id: str = "t1") -> Any:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": CHECKPOINT["id"]}}


def _tuple(thread_id: str = "t1") -> CheckpointTuple:
    return CheckpointTuple(config=_config(thread_id), checkpoint=CHECKPOINT, metadata={}, parent_config=None)


class FakeSaver(BaseCheckpointSaver):
    """An inner saver recording each async call; ``fail`` makes every call raise."""

    def __init__(self) -> None:
        super().__init__(serde=JsonPlusSerializer())
        self.calls: list[str] = []
        self.fail: Exception | None = None

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail is not None:
            raise self.fail

    @property
    def config_specs(self) -> list:
        return ["spec"]

    def get_next_version(self, current: Any, channel: None) -> Any:
        return f"next-{current}"

    async def aget_tuple(self, config: Any) -> CheckpointTuple | None:
        self._call("aget_tuple")
        return _tuple()

    async def alist(
        self,
        config: Any,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 -- the library's parameter name
        before: Any = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        self._call("alist")
        yield _tuple("t1")
        yield _tuple("t2")

    async def aput(
        self, config: Any, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions: ChannelVersions
    ) -> Any:
        self._call("aput")
        return config

    async def aput_writes(
        self, config: Any, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        self._call("aput_writes")

    async def adelete_thread(self, thread_id: str) -> None:
        self._call("adelete_thread")

    async def adelete_for_runs(self, run_ids: Any) -> None:
        self._call("adelete_for_runs")

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        self._call("acopy_thread")

    async def aprune(self, thread_ids: Any, *, strategy: str = "keep_latest") -> None:
        self._call("aprune")

    async def aget_delta_channel_history(
        self, *, config: Any, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        self._call("aget_delta_channel_history")
        return {}

    def get_tuple(self, config: Any) -> CheckpointTuple | None:
        self.calls.append("get_tuple")
        return _tuple()


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter) -> Iterator[OtelWriter]:
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    w = OtelWriter(exporter=exporter)
    app = SimpleNamespace(monitoring=SimpleNamespace(active=SimpleNamespace(writer=w)))
    with tai42_app.bound(app):
        yield w
    w.shutdown()


@pytest.fixture
def inner() -> FakeSaver:
    return FakeSaver()


@pytest.fixture
def saver(inner: FakeSaver) -> TimedSaver:
    return TimedSaver(inner, provider=PROVIDER)


def _observations(writer: OtelWriter, exporter: InMemorySpanExporter) -> list[MonitoringObservation]:
    writer.flush()
    return [span_to_observation(s) for s in exporter.get_finished_spans()]


async def _drain(items: AsyncIterator[CheckpointTuple]) -> list[CheckpointTuple]:
    return [item async for item in items]


# Each async door, the call that drives it, the span it records and the thread it names.
DOORS: list[tuple[str, Any, str, str | None]] = [
    ("aget_tuple", lambda s: s.aget_tuple(_config()), "checkpoint:get_tuple", "t1"),
    ("aput", lambda s: s.aput(_config(), CHECKPOINT, {}, {}), "checkpoint:put", "t1"),
    ("aput_writes", lambda s: s.aput_writes(_config(), [("c", 1)], "task"), "checkpoint:put_writes", "t1"),
    ("adelete_thread", lambda s: s.adelete_thread("t1"), "checkpoint:delete_thread", "t1"),
    ("adelete_for_runs", lambda s: s.adelete_for_runs(["r1"]), "checkpoint:delete_for_runs", None),
    ("acopy_thread", lambda s: s.acopy_thread("t1", "t2"), "checkpoint:copy_thread", None),
    ("aprune", lambda s: s.aprune(["t1"]), "checkpoint:prune", None),
    (
        "aget_delta_channel_history",
        lambda s: s.aget_delta_channel_history(config=_config(), channels=["c"]),
        "checkpoint:delta_history",
        "t1",
    ),
    ("alist", lambda s: _drain(s.alist(_config())), "checkpoint:list", "t1"),
]


@pytest.mark.parametrize(("method", "call", "span_name", "thread_id"), DOORS, ids=[d[0] for d in DOORS])
async def test_each_door_records_one_internal_span_under_the_current_span(
    writer: OtelWriter,
    exporter: InMemorySpanExporter,
    saver: TimedSaver,
    inner: FakeSaver,
    method: str,
    call: Any,
    span_name: str,
    thread_id: str | None,
) -> None:
    with writer.start_span(name="graph", kind=SpanKind.CHAIN) as parent:
        await call(saver)
    assert inner.calls == [method]
    by_name = {o.name: o for o in _observations(writer, exporter)}
    assert set(by_name) == {"graph", span_name}
    span = by_name[span_name]
    assert span.parent_id == parent.id
    assert span.kind is SpanKind.CHAIN
    assert span.metadata == {
        STEP_ROLE_METADATA_KEY: StepRole.INTERNAL.value,
        "checkpoint.provider": PROVIDER,
        "checkpoint.thread_id": thread_id,
    }
    assert span.input is None
    assert span.output is None
    assert span.end is not None


async def test_aget_records_one_span(writer: OtelWriter, exporter: InMemorySpanExporter, saver: TimedSaver) -> None:
    with writer.start_span(name="graph", kind=SpanKind.CHAIN):
        checkpoint = await saver.aget(_config())
    assert checkpoint == CHECKPOINT
    assert sorted(o.name or "" for o in _observations(writer, exporter)) == ["checkpoint:get_tuple", "graph"]


@pytest.mark.parametrize(("method", "call", "span_name", "thread_id"), DOORS, ids=[d[0] for d in DOORS])
async def test_with_no_live_trace_a_door_calls_the_inner_and_records_nothing(
    writer: OtelWriter,
    exporter: InMemorySpanExporter,
    saver: TimedSaver,
    inner: FakeSaver,
    method: str,
    call: Any,
    span_name: str,
    thread_id: str | None,
) -> None:
    await call(saver)
    assert inner.calls == [method]
    assert _observations(writer, exporter) == []


@pytest.mark.parametrize(("method", "call", "span_name", "thread_id"), DOORS, ids=[d[0] for d in DOORS])
async def test_an_inner_failure_ends_the_span_at_error_and_propagates(
    writer: OtelWriter,
    exporter: InMemorySpanExporter,
    saver: TimedSaver,
    inner: FakeSaver,
    method: str,
    call: Any,
    span_name: str,
    thread_id: str | None,
) -> None:
    inner.fail = RuntimeError("store down")
    with writer.start_span(name="graph", kind=SpanKind.CHAIN), pytest.raises(RuntimeError, match="store down"):
        await call(saver)
    span = {o.name: o for o in _observations(writer, exporter)}[span_name]
    assert span.level == "ERROR"
    assert span.status_message == "store down"


async def test_alist_yields_the_inner_items_unchanged_under_one_span(
    writer: OtelWriter, exporter: InMemorySpanExporter, saver: TimedSaver
) -> None:
    with writer.start_span(name="graph", kind=SpanKind.CHAIN):
        items = await _drain(saver.alist(_config()))
    assert items == [_tuple("t1"), _tuple("t2")]
    assert [o.name for o in _observations(writer, exporter)].count("checkpoint:list") == 1


async def test_alist_left_early_still_ends_its_span(
    writer: OtelWriter, exporter: InMemorySpanExporter, saver: TimedSaver
) -> None:
    with writer.start_span(name="graph", kind=SpanKind.CHAIN):
        items = saver.alist(_config())
        first = await anext(items)
        await items.aclose()  # pyright: ignore[reportAttributeAccessIssue]
    assert first == _tuple("t1")
    assert [o.name for o in _observations(writer, exporter)].count("checkpoint:list") == 1


async def test_a_put_submitted_from_a_copied_context_parents_to_the_span_current_at_submit(
    writer: OtelWriter, exporter: InMemorySpanExporter, saver: TimedSaver
) -> None:
    with writer.start_span(name="graph", kind=SpanKind.CHAIN) as parent:
        task = asyncio.get_running_loop().create_task(
            saver.aput(_config(), CHECKPOINT, {}, {}), context=contextvars.copy_context()
        )
        with writer.start_span(name="next", kind=SpanKind.CHAIN):
            await task
    span = {o.name: o for o in _observations(writer, exporter)}["checkpoint:put"]
    assert span.parent_id == parent.id


async def test_the_saver_answers_with_the_inner_serializer_specs_and_versions(
    saver: TimedSaver, inner: FakeSaver
) -> None:
    assert saver.serde is inner.serde
    assert saver.config_specs == ["spec"]
    assert saver.get_next_version(1, None) == "next-1"


def test_a_synchronous_door_is_the_inner_saver_own(
    writer: OtelWriter, exporter: InMemorySpanExporter, saver: TimedSaver, inner: FakeSaver
) -> None:
    assert saver.get_tuple(_config()) == _tuple()
    assert inner.calls == ["get_tuple"]
    assert _observations(writer, exporter) == []
