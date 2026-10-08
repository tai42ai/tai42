"""A neutral, non-vendor monitoring backend proving another plugin can use the
monitoring read contract AS IT IS.

The backend aggregates an in-memory store into the typed contract shapes: it
declares the neutral measures and dimensions it serves, maps its own columns onto
:class:`MetricsRow`, and raises on a measure or dimension it does not declare — no
vendor vocabulary anywhere. The tests drive it both directly and through the
skeleton observability doors (the platform consumer), proving the platform reads
nothing vendor-specific out of it and never a silent zero / silent-unavailable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from starlette.requests import Request
from tai42_contract.monitoring import (
    Dimension,
    Measure,
    MetricsCapability,
    MetricsQuery,
    MetricsResult,
    MetricsRow,
    MonitoringObservation,
    MonitoringReadNotSupportedError,
    MonitoringTrace,
    TraceNotFoundError,
)

from tai42_skeleton.monitoring.noop import NoOpWriter
from tai42_skeleton.monitoring.registry import register_monitoring, reset_monitoring
from tai42_skeleton.routers import observability as router

_FULL = MetricsCapability(
    measures=frozenset({Measure.COUNT, Measure.COST, Measure.TOKENS, Measure.LATENCY}),
    dimensions=frozenset({Dimension.MODEL}),
)


@dataclass
class _Run:
    """One record in the neutral store (the backend's own shape, no vendor names)."""

    bucket: str
    model: str
    cost: float
    tokens: int
    latency_ms: float


@dataclass
class _NeutralReader:
    """Aggregates the in-memory runs into typed contract rows."""

    runs: list[_Run] = field(default_factory=list)
    traces: dict[str, MonitoringTrace] = field(default_factory=dict)
    capability: MetricsCapability = _FULL

    def metrics_capability(self) -> MetricsCapability:
        return self.capability

    async def query_metrics(self, query: MetricsQuery) -> MetricsResult:
        unserved_m = [m for m in query.measures if m not in self.capability.measures]
        unserved_d = [d for d in query.dimensions if d not in self.capability.dimensions]
        if unserved_m or unserved_d:
            raise MonitoringReadNotSupportedError(
                f"neutral backend does not serve measures={unserved_m} dimensions={unserved_d}"
            )
        if Dimension.MODEL in query.dimensions:
            groups: dict[str, list[_Run]] = {}
            for run in self.runs:
                groups.setdefault(run.model, []).append(run)
            rows = [self._row(query, group, model=model) for model, group in sorted(groups.items())]
        elif query.granularity:
            buckets: dict[str, list[_Run]] = {}
            for run in self.runs:
                buckets.setdefault(run.bucket, []).append(run)
            rows = [self._row(query, group, bucket=bucket) for bucket, group in sorted(buckets.items())]
        else:
            rows = [self._row(query, self.runs)] if self.runs else []
        return MetricsResult(rows=rows)

    @staticmethod
    def _row(
        query: MetricsQuery, group: list[_Run], *, model: str | None = None, bucket: str | None = None
    ) -> MetricsRow:
        values: dict[Measure, float | None] = {}
        for measure in query.measures:
            if measure is Measure.COUNT:
                values[measure] = float(len(group))
            elif measure is Measure.COST:
                values[measure] = sum(r.cost for r in group)
            elif measure is Measure.TOKENS:
                values[measure] = float(sum(r.tokens for r in group))
            elif measure is Measure.LATENCY:
                values[measure] = (sum(r.latency_ms for r in group) / len(group)) if group else None
        dimensions = {Dimension.MODEL: model} if Dimension.MODEL in query.dimensions else {}
        return MetricsRow(dimensions=dimensions, measures=values, bucket=bucket)

    async def get_trace(self, trace_id: str) -> MonitoringTrace:
        trace = self.traces.get(trace_id)
        if trace is None:
            raise TraceNotFoundError(trace_id)
        return trace

    async def list_traces(self, **_kwargs: Any) -> list[Any]:
        return []

    async def get_observation(self, trace_id: str, observation_id: str) -> MonitoringObservation:
        for observation in (await self.get_trace(trace_id)).observations:
            if observation.id == observation_id:
                return observation
        raise TraceNotFoundError(trace_id)

    async def list_spans_in_window(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []


class _NeutralWriter(NoOpWriter):
    pass


@dataclass
class _NeutralMonitoring:
    reader_obj: _NeutralReader

    @property
    def reader(self) -> _NeutralReader:
        return self.reader_obj

    @property
    def writer(self) -> _NeutralWriter:
        return _NeutralWriter()


def _install(reader: _NeutralReader) -> _NeutralReader:
    register_monitoring(lambda: _NeutralMonitoring(reader))
    return reader


@pytest.fixture(autouse=True)
def _reset_backend():
    reset_monitoring()
    yield
    reset_monitoring()


def _req(query: str = "", **path_params) -> Request:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/observability",
        "headers": [],
        "query_string": query.encode(),
        "path_params": path_params,
    }
    return Request(scope, receive)


def _data(resp) -> dict:
    return json.loads(bytes(resp.body))["data"]


# --- the backend serves its declared vocabulary, mapped onto the typed rows -------


async def test_query_metrics_maps_declared_measures_and_dimensions():
    reader = _NeutralReader(
        runs=[
            _Run(bucket="2026-07-01", model="m-a", cost=1.0, tokens=100, latency_ms=200),
            _Run(bucket="2026-07-01", model="m-b", cost=0.5, tokens=50, latency_ms=400),
        ]
    )
    ungrouped = await reader.query_metrics(
        MetricsQuery(measures=list(_FULL.measures), from_timestamp=datetime.now(UTC), to_timestamp=datetime.now(UTC))
    )
    row = ungrouped.rows[0]
    assert row.measures[Measure.COUNT] == 2.0
    assert row.measures[Measure.COST] == 1.5
    assert row.measures[Measure.TOKENS] == 150.0
    assert row.measures[Measure.LATENCY] == 300.0

    grouped = await reader.query_metrics(
        MetricsQuery(
            measures=[Measure.COUNT, Measure.COST],
            from_timestamp=datetime.now(UTC),
            to_timestamp=datetime.now(UTC),
            dimensions=[Dimension.MODEL],
        )
    )
    assert {r.dimensions[Dimension.MODEL] for r in grouped.rows} == {"m-a", "m-b"}


async def test_query_metrics_raises_on_undeclared_measure():
    reader = _NeutralReader(capability=MetricsCapability(measures=frozenset({Measure.COUNT}), dimensions=frozenset()))
    with pytest.raises(MonitoringReadNotSupportedError):
        await reader.query_metrics(
            MetricsQuery(measures=[Measure.LATENCY], from_timestamp=datetime.now(UTC), to_timestamp=datetime.now(UTC))
        )


async def test_query_metrics_raises_on_undeclared_dimension():
    reader = _NeutralReader(capability=MetricsCapability(measures=frozenset(Measure), dimensions=frozenset()))
    with pytest.raises(MonitoringReadNotSupportedError):
        await reader.query_metrics(
            MetricsQuery(
                measures=[Measure.COUNT],
                from_timestamp=datetime.now(UTC),
                to_timestamp=datetime.now(UTC),
                dimensions=[Dimension.MODEL],
            )
        )


# --- the platform drives the neutral backend end-to-end --------------------------


async def test_skeleton_metrics_door_consumes_the_neutral_backend():
    _install(
        _NeutralReader(
            runs=[
                _Run(bucket="2026-07-01", model="m-a", cost=2.0, tokens=200, latency_ms=100),
                _Run(bucket="2026-07-01", model="m-b", cost=1.0, tokens=100, latency_ms=300),
            ]
        )
    )
    data = _data(await router.get_metrics(_req("")))
    assert data["summary"]["totalRuns"] == 2
    assert data["summary"]["totalCost"] == 3.0
    assert data["byModelAvailable"] is True
    assert {row["model"] for row in data["byModel"]} == {"m-a", "m-b"}


async def test_skeleton_by_model_absent_when_backend_does_not_declare_it():
    _install(
        _NeutralReader(
            runs=[_Run(bucket="2026-07-01", model="m-a", cost=2.0, tokens=200, latency_ms=100)],
            capability=MetricsCapability(measures=frozenset(Measure), dimensions=frozenset()),
        )
    )
    data = _data(await router.get_metrics(_req("")))
    assert data["byModelAvailable"] is False
    assert data["byModel"] == []
    assert data["summary"]["totalRuns"] == 1


async def test_trace_view_passes_metadata_whole():
    reader = _install(_NeutralReader())
    span_metadata = {"node_id": "n1", "anything": {"nested": True}}
    reader.traces["t1"] = MonitoringTrace(
        id="t1",
        timestamp=datetime(2026, 7, 1, tzinfo=UTC),
        metadata={"trace_level": "kept"},
        observations=[MonitoringObservation(id="o1", trace_id="t1", metadata=span_metadata)],
    )
    data = _data(await router.get_run_trace(_req("", trace_id="t1")))
    # The platform reads NO key out of metadata — it passes through whole, and the
    # span carries no lifted consumer field.
    assert data["metadata"] == {"trace_level": "kept"}
    assert data["spans"][0]["metadata"] == span_metadata
    assert "nodeId" not in data["spans"][0]
