"""The run read surface: ``get_trace``, ``get_observation``, ``list_traces`` and ``export_traces``.

A run is a trace: the observations sharing a trace id. Its root (the one observation marked
``isRootObservation``) carries the run's start, name, tags, user, session, version, latency, input and
output; cost, tokens, level and model come from every observation of the trace. A list reads one of three
ways: newest first with only run-level clauses walks the roots; a cost or token rank with only the name and
tags clauses asks one top-N metrics query; every other sort or clause reads the roots of the time range and
ranks them in the plugin. Each way has a bound it answers ``MonitoringReadTooBroadError`` past.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from tai42_contract.monitoring import (
    MonitoringError,
    MonitoringFilter,
    MonitoringObservation,
    MonitoringReadTooBroadError,
    MonitoringTrace,
    MonitoringTraceSummary,
    ObservationNotFoundError,
    OrderBy,
    TraceNotFoundError,
)

from tai42_monitoring_langfuse.filters import (
    ROOT_CLAUSE,
    Clause,
    environment_clause,
    has_root_only_clauses,
    level_value,
    ranked_metric_clauses,
    run_level_clauses,
    run_wide_filters,
    string_clause,
    trace_ids_clause,
    window_clauses,
)
from tai42_monitoring_langfuse.measures import _measure_value, _nonneg_number
from tai42_monitoring_langfuse.observation_query import (
    FULL_FIELDS,
    ObservationQuery,
    ObservationRow,
    decode_io,
    observation_kind,
    producer_metadata,
    token_counts,
    total_tokens,
)
from tai42_monitoring_langfuse.query_base import (
    ENRICH_MAX_OBSERVATIONS,
    NATIVE_MAX_REACH,
    PAGE_SIZE,
    RANKED_MAX_REACH,
    TRACE_ID_CHUNK,
    WINDOW_MAX_ROOTS,
)
from tai42_monitoring_langfuse.sorting import RANKED_MEASURE, _none_last_sorted, resolve_trace_sort

# The field groups of a root row: its run fields, without its input and output.
_ROOT_FIELDS = "core,basic,metrics,trace_context"
_DETAIL_FIELDS = "core,io"
_ENRICH_FIELDS = "core,basic,usage,model"

_NATIVE_TOO_BROAD = f"Paging past run {NATIVE_MAX_REACH:,} is not served. Narrow the time range."
_NO_START_TOO_BROAD = "A ranked list needs a start time."
_RANKED_TOO_BROAD = f"A ranked list pages up to run {RANKED_MAX_REACH:,}. Narrow the time range or add filters."
_ENRICH_TOO_BROAD = (
    f"The runs in this time range carry more than {ENRICH_MAX_OBSERVATIONS:,} observations. "
    "Narrow the time range or add filters."
)
_SORT_NOUN = {
    "timestamp": "time",
    "total_cost": "cost",
    "total_tokens": "tokens",
    "latency": "latency",
    "name": "name",
    "id": "run id",
}
_ASCENDING_SUFFIX = {"timestamp": ", oldest first", "total_cost": ", lowest first", "total_tokens": ", lowest first"}
_ASCENDING_SUFFIX["latency"] = ", lowest first"


@dataclass
class _Run:
    """One run being read: its root row, its decoded input and output, and its run-wide totals."""

    root: ObservationRow
    input: Any = None
    output: Any = None
    enriched: bool = False
    cost: float | None = None
    tokens: int | None = None
    levels: set[str] = field(default_factory=set[str])
    models: set[str] = field(default_factory=set[str])

    @property
    def trace_id(self) -> str:
        return self.root.trace_id

    @property
    def name(self) -> str | None:
        return self.root.trace_name or self.root.name

    @property
    def latency_ms(self) -> float | None:
        latency = self.root.latency
        return latency * 1000.0 if _nonneg_number(latency) else None

    def sort_key(self, field_name: str) -> Any:
        return {
            "timestamp": self.root.start_time,
            "total_cost": self.cost,
            "total_tokens": self.tokens,
            "latency": self.latency_ms,
            "name": self.name,
            "id": self.trace_id,
        }[field_name]

    def summary(self) -> MonitoringTraceSummary:
        return MonitoringTraceSummary(
            id=self.trace_id,
            timestamp=self.root.start_time,
            name=self.name,
            tags=list(self.root.tags or []),
            input=self.input,
            output=self.output,
            latency_ms=self.latency_ms,
            total_cost=self.cost,
            total_tokens=self.tokens,
            status="error" if "ERROR" in self.levels else "ok",
        )


def _in_range(value: float | None, low: float | None, high: float | None) -> bool:
    if low is None and high is None:
        return True
    if value is None:
        return False
    return (low is None or value >= low) and (high is None or value <= high)


def _matches_run_wide(run: _Run, filter_: MonitoringFilter | None) -> bool:
    """Whether an enriched run passes the run-wide clauses; a run with no recorded total matches no range on it."""
    if filter_ is None:
        return True
    if not _in_range(run.cost, filter_.min_cost, filter_.max_cost):
        return False
    if not _in_range(run.tokens, filter_.min_tokens, filter_.max_tokens):
        return False
    if filter_.level is not None and level_value(filter_.level) not in run.levels:
        return False
    return filter_.model is None or filter_.model in run.models


def _chunks(trace_ids: Sequence[str]) -> Iterator[list[str]]:
    for start in range(0, len(trace_ids), TRACE_ID_CHUNK):
        yield list(trace_ids[start : start + TRACE_ID_CHUNK])


def _window_purpose(order_by: OrderBy, run_wide: list[str]) -> str:
    if run_wide:
        return f"apply the {', '.join(run_wide)} filter{'s' if len(run_wide) > 1 else ''}"
    suffix = _ASCENDING_SUFFIX.get(order_by.field, "") if order_by.direction == "asc" else ""
    return f"rank by {_SORT_NOUN[order_by.field]}{suffix}"


class TraceQuery(ObservationQuery):
    """Serves the run read surface from the Langfuse observations list and Metrics v2."""

    async def get_trace(self, trace_id: str) -> MonitoringTrace:
        """Every observation of ``trace_id``; zero rows raise ``TraceNotFoundError``, any other failure propagates."""
        client = await self._active_client()
        rows = await self._walk_all(
            client, fields=FULL_FIELDS, clauses=[string_clause("traceId", trace_id)], expand_metadata=True
        )
        if not rows:
            raise TraceNotFoundError(f"trace {trace_id} not found")
        root = next((row for row in rows if row.is_root_observation), None)
        costs = [row.total_cost for row in rows if _nonneg_number(row.total_cost)]
        observations = sorted((self._map_observation(row) for row in rows), key=lambda o: (o.start, o.id))
        return MonitoringTrace(
            id=trace_id,
            timestamp=root.start_time if root is not None else observations[0].start,
            tags=list(root.tags or []) if root is not None else [],
            input=decode_io(root.input) if root is not None else None,
            output=decode_io(root.output) if root is not None else None,
            metadata=root.metadata if root is not None else None,
            total_cost=sum(costs) if costs else None,
            observations=observations,
        )

    async def get_observation(self, trace_id: str, observation_id: str) -> MonitoringObservation:
        """One observation of ``trace_id``.

        An absent trace raises ``TraceNotFoundError``, an absent observation ``ObservationNotFoundError``; any
        other failure propagates.
        """
        client = await self._active_client()
        rows, _ = await self._list_page(
            client,
            fields=FULL_FIELDS,
            clauses=[string_clause("traceId", trace_id), string_clause("id", observation_id)],
            expand_metadata=True,
        )
        if rows:
            return self._map_observation(rows[0])
        trace_rows, _ = await self._list_page(
            client, fields="core", clauses=[string_clause("traceId", trace_id)], limit=1
        )
        if not trace_rows:
            raise TraceNotFoundError(f"trace {trace_id} not found")
        raise ObservationNotFoundError(f"observation {observation_id} not found in trace {trace_id}")

    async def list_traces(
        self,
        *,
        from_timestamp: datetime | None = None,
        to_timestamp: datetime | None = None,
        limit: int | None = None,
        page: int | None = None,
        filter_: MonitoringFilter | None = None,
        order_by: OrderBy | None = None,
    ) -> list[MonitoringTraceSummary]:
        """One page of run summaries; ``limit`` defaults to the largest page, ``page`` to the first."""
        size = PAGE_SIZE if limit is None else limit
        number = 1 if page is None else page
        if size > PAGE_SIZE:
            raise ValueError(f"limit {size} exceeds the reader's maximum page size {PAGE_SIZE}")
        if size < 1:
            raise ValueError(f"limit {size} must be 1 or more")
        if number < 1:
            raise ValueError(f"page {number} must be 1 or more")
        return await self._read_runs(
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            start=(number - 1) * size,
            reach=number * size,
            filter_=filter_,
            order_by=order_by,
            export=False,
        )

    async def export_traces(
        self,
        *,
        from_timestamp: datetime | None = None,
        to_timestamp: datetime | None = None,
        max_rows: int,
        filter_: MonitoringFilter | None = None,
        order_by: OrderBy | None = None,
    ) -> list[MonitoringTraceSummary]:
        """Up to ``max_rows + 1`` run summaries in the ``list_traces`` order, in one read."""
        if max_rows < 0:
            raise ValueError(f"max_rows {max_rows} must not be negative")
        return await self._read_runs(
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            start=0,
            reach=max_rows + 1,
            filter_=filter_,
            order_by=order_by,
            export=True,
        )

    async def _read_runs(
        self,
        *,
        from_timestamp: datetime | None,
        to_timestamp: datetime | None,
        start: int,
        reach: int,
        filter_: MonitoringFilter | None,
        order_by: OrderBy | None,
        export: bool,
    ) -> list[MonitoringTraceSummary]:
        """The runs ``[start, reach)`` of the sorted, filtered list, with their input, output and totals.

        An export reads up to ``reach`` runs: the newest-first and ranked page bounds do not refuse it up front.
        """
        sort = resolve_trace_sort(order_by)
        client = await self._active_client()
        source = self._m.active_source()
        run_wide = run_wide_filters(filter_)
        if sort.field == "timestamp" and sort.direction == "desc" and not run_wide:
            if not export and reach > NATIVE_MAX_REACH:
                raise MonitoringReadTooBroadError(_NATIVE_TOO_BROAD)
            runs = await self._native_runs(client, source, from_timestamp, to_timestamp, reach, filter_)
        elif (
            sort.field in RANKED_MEASURE
            and sort.direction == "desc"
            and not run_wide
            and not has_root_only_clauses(filter_)
        ):
            runs = await self._ranked_runs(
                client, source, from_timestamp, to_timestamp, reach, filter_, sort.field, export=export
            )
        else:
            runs = await self._window_runs(client, source, from_timestamp, to_timestamp, filter_, sort, run_wide)
        page = runs[start:reach]
        if page:
            await self._detail(client, source, page)
            await self._enrich(client, source, [run for run in page if not run.enriched])
        return [run.summary() for run in page]

    def _root_clauses(
        self, source: str, t0: datetime | None, t1: datetime | None, filter_: MonitoringFilter | None
    ) -> list[Clause]:
        return [environment_clause(source), ROOT_CLAUSE, *window_clauses(t0, t1), *run_level_clauses(filter_)]

    async def _native_runs(
        self,
        client: httpx.Client,
        source: str,
        t0: datetime | None,
        t1: datetime | None,
        reach: int,
        filter_: MonitoringFilter | None,
    ) -> list[_Run]:
        """The newest ``reach`` roots, plus every root sharing the last one's start, by start then trace id."""
        roots: list[ObservationRow] = []
        walk = self._walk(client, fields=_ROOT_FIELDS, clauses=self._root_clauses(source, t0, t1, filter_))
        async with aclosing(walk) as rows:
            async for row in rows:
                if len(roots) >= reach and row.start_time != roots[reach - 1].start_time:
                    break
                roots.append(row)
        roots.sort(key=lambda row: (row.start_time, row.trace_id), reverse=True)
        return [_Run(root) for root in roots]

    async def _ranked_runs(
        self,
        client: httpx.Client,
        source: str,
        t0: datetime | None,
        t1: datetime | None,
        reach: int,
        filter_: MonitoringFilter | None,
        field_name: str,
        *,
        export: bool,
    ) -> list[_Run]:
        """The runs ranked by one top-N metrics query over the time range, whose roots started in it."""
        if t0 is None:
            raise MonitoringReadTooBroadError(_NO_START_TOO_BROAD)
        if not export and reach > RANKED_MAX_REACH:
            raise MonitoringReadTooBroadError(_RANKED_TOO_BROAD)
        measure = RANKED_MEASURE[field_name]
        rows = await self._metrics(
            client,
            {
                "view": "observations",
                "dimensions": [{"field": "traceId"}],
                "metrics": [{"measure": measure, "aggregation": "sum"}],
                "filters": [environment_clause(source), *ranked_metric_clauses(filter_)],
                "fromTimestamp": t0.isoformat(),
                "toTimestamp": (t1 or datetime.now(UTC)).isoformat(),
                "orderBy": [{"field": f"sum_{measure}", "direction": "desc"}],
                "config": {"row_limit": RANKED_MAX_REACH},
            },
        )
        column = f"sum_{measure}"
        ranked: list[tuple[float, str]] = []
        for row in rows:
            if column not in row or "traceId" not in row:
                raise MonitoringError(f"Langfuse metrics response row carries no traceId or {column} column: {row!r}")
            ranked.append((_measure_value(row[column]) or 0.0, row["traceId"]))
        ranked.sort(reverse=True)
        roots = await self._roots_of(client, source, [trace_id for _, trace_id in ranked])
        runs = [
            _Run(roots[trace_id])
            for _, trace_id in ranked
            if trace_id in roots
            and roots[trace_id].start_time >= t0
            and (t1 is None or roots[trace_id].start_time < t1)
        ]
        if len(runs) < reach and len(rows) >= RANKED_MAX_REACH:
            raise MonitoringReadTooBroadError(_RANKED_TOO_BROAD)
        return runs

    async def _roots_of(self, client: httpx.Client, source: str, trace_ids: list[str]) -> dict[str, ObservationRow]:
        roots: dict[str, ObservationRow] = {}
        for chunk in _chunks(trace_ids):
            clauses = [environment_clause(source), ROOT_CLAUSE, trace_ids_clause(chunk)]
            for row in await self._walk_all(client, fields=_ROOT_FIELDS, clauses=clauses):
                roots[row.trace_id] = row
        return roots

    async def _window_runs(
        self,
        client: httpx.Client,
        source: str,
        t0: datetime | None,
        t1: datetime | None,
        filter_: MonitoringFilter | None,
        sort: OrderBy,
        run_wide: list[str],
    ) -> list[_Run]:
        """Every run whose root started in the time range, filtered and sorted in the plugin."""
        if t0 is None:
            raise MonitoringReadTooBroadError(_NO_START_TOO_BROAD)
        roots: list[ObservationRow] = []
        walk = self._walk(client, fields=_ROOT_FIELDS, clauses=self._root_clauses(source, t0, t1, filter_))
        async with aclosing(walk) as rows:
            async for row in rows:
                roots.append(row)
                if len(roots) > WINDOW_MAX_ROOTS:
                    raise MonitoringReadTooBroadError(
                        f"This list reads at most {WINDOW_MAX_ROOTS:,} runs in the time range to "
                        f"{_window_purpose(sort, run_wide)}, and this range holds more. "
                        "Narrow the time range or add filters."
                    )
        runs = [_Run(root) for root in roots]
        if run_wide or sort.field in RANKED_MEASURE:
            await self._enrich(client, source, runs)
            runs = [run for run in runs if _matches_run_wide(run, filter_)]
        reverse = sort.direction == "desc"
        runs.sort(key=lambda run: run.trace_id, reverse=reverse)
        return _none_last_sorted(runs, lambda run: run.sort_key(sort.field), reverse)

    async def _detail(self, client: httpx.Client, source: str, runs: list[_Run]) -> None:
        """Fill each run's input and output from its root."""
        by_id = {run.trace_id: run for run in runs}
        for chunk in _chunks(list(by_id)):
            clauses = [environment_clause(source), ROOT_CLAUSE, trace_ids_clause(chunk)]
            for row in await self._walk_all(client, fields=_DETAIL_FIELDS, clauses=clauses):
                run = by_id.get(row.trace_id)
                if run is not None:
                    run.input = decode_io(row.input)
                    run.output = decode_io(row.output)

    async def _enrich(self, client: httpx.Client, source: str, runs: list[_Run]) -> None:
        """Total each run's cost and tokens and collect its levels and models from every observation of its trace."""
        by_id = {run.trace_id: run for run in runs}
        read = 0
        for chunk in _chunks(list(by_id)):
            clauses = [environment_clause(source), trace_ids_clause(chunk)]
            async with aclosing(self._walk(client, fields=_ENRICH_FIELDS, clauses=clauses)) as rows:
                async for row in rows:
                    read += 1
                    if read > ENRICH_MAX_OBSERVATIONS:
                        raise MonitoringReadTooBroadError(_ENRICH_TOO_BROAD)
                    run = by_id.get(row.trace_id)
                    if run is None:
                        raise MonitoringError(f"Langfuse returned an observation of unrequested trace {row.trace_id}")
                    self._add_observation(run, row)
        for run in runs:
            run.enriched = True

    @staticmethod
    def _add_observation(run: _Run, row: ObservationRow) -> None:
        if _nonneg_number(row.total_cost):
            run.cost = (run.cost or 0.0) + row.total_cost
        tokens = total_tokens(row.usage_details)
        if tokens is not None:
            run.tokens = (run.tokens or 0) + tokens
        if row.level is not None:
            run.levels.add(row.level)
        if row.model:
            run.models.add(row.model)

    @staticmethod
    def _map_observation(row: ObservationRow) -> MonitoringObservation:
        return MonitoringObservation(
            id=row.id,
            trace_id=row.trace_id,
            parent_id=row.parent_observation_id,
            kind=observation_kind(row.type),
            name=row.name,
            level=row.level,
            status_message=row.status_message,
            input=decode_io(row.input),
            output=decode_io(row.output),
            metadata=producer_metadata(row.metadata, row.id),
            model=row.model,
            **token_counts(row.usage_details),
            start=row.start_time,
            end=row.end_time,
        )
