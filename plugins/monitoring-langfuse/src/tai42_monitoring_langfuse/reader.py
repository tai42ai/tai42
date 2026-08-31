"""The Langfuse read/query implementation of ``MonitoringReader``.

- ``query_metrics`` -> the Metrics API.
- ``list_spans_in_window`` -> the Observations API (one item per tool/node run).
- ``get_trace`` / ``list_traces`` -> the Traces API.

The reader methods are ``async`` but the Langfuse client is synchronous, so every
blocking call is dispatched via ``asyncio.to_thread``. Every read is scoped to the
active project's ``source`` (the Langfuse ``environment``) so a shared project
returns only our rows; ``get_trace`` is exempt, since a trace id is globally unique.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, TypeGuard

from langfuse.api.commons.errors import NotFoundError
from tai42_contract.monitoring import (
    MetricsFilter,
    MetricsResult,
    MetricsRow,
    MonitoringFilter,
    MonitoringObservation,
    MonitoringReadNotSupportedError,
    MonitoringTrace,
    MonitoringTraceSummary,
    OrderBy,
    SpanKind,
    SpanWindowItem,
    TraceNotFoundError,
    preview,
)

from tai42_monitoring_langfuse.client_manager import LangfuseClientManager

logger = logging.getLogger(__name__)

# Natural aggregation per measure when the caller names a measure without one.
_DEFAULT_AGGREGATION: dict[str, str] = {
    "count": "count",
    "totalCost": "sum",
    "totalTokens": "sum",
    "latency": "avg",
    "timeToFirstToken": "avg",
}

# Observation types that are never a tool/node execution.
_EXCLUDED_TYPES = {"GENERATION", "EVENT", "TRACE"}
# Grouping chains emitted around the real work, not work themselves.
_GROUPING_NAMES = {"tools", "model"}

# Neutral SpanKind -> Langfuse observation type, for the optional ``kind``
# narrowing within the tool-granularity set.
_KIND_TO_TYPE: dict[SpanKind, str] = {
    SpanKind.LLM: "GENERATION",
    SpanKind.TOOL: "TOOL",
    SpanKind.CHAIN: "SPAN",
    SpanKind.EVENT: "EVENT",
}

_PAGE_SIZE = 100

# --- sort keys ---------------------------------------------------------------
# Neutral OrderBy.field -> Langfuse trace.list native sort field (server-side).
# timestamp/name/id sort natively; total_cost/latency/total_tokens have no
# native trace sort and are ranked GLOBALLY via the metrics API instead.
_TRACE_NATIVE_SORT: dict[str, str] = {"timestamp": "timestamp", "name": "name", "id": "id"}
# Neutral OrderBy.field -> Langfuse metrics measure, for the global metric rank.
_TRACE_METRIC_MEASURE: dict[str, str] = {
    "total_cost": "totalCost",
    "latency": "latency",
    "total_tokens": "totalTokens",
}
_TRACE_SORT_FIELDS = {"timestamp", "total_cost", "name", "id", "latency", "total_tokens"}
# The metrics traces view server-enforces config.row_limit in [1, 1000].
_METRIC_ROW_LIMIT_MAX = 1000
# get_many has no native sort -> every span-window sort is client-side.
_SPAN_SORT_FIELDS = {"start", "end", "duration", "name", "id"}

# The trace.list field groups a row summary needs: core attributes, io (input,
# output, metadata) and metrics (latency, total_cost). Observation bodies are
# never listed — get_trace is the only body door.
_LIST_FIELDS = "core,io,metrics"
# Buffer added to a page's newest timestamp so a trace/observation exactly at the
# upper bound is caught by the backend's exclusive upper bound.
_WINDOW_EPSILON = timedelta(seconds=1)
# Max trace.list pages walked to cover a metric-sorted page's ranked ids before
# raising loudly — the ranked ids scatter across the whole window, so a wide
# window with the ranked traces at its far end could otherwise page unbounded.
_METRIC_LIST_PAGE_BUDGET = 20
# Max observation pages walked to collect a page's error ids before raising
# loudly — errors are rare so the window normally resolves in one call; a wide
# window with many errors could otherwise page unbounded.
_ERROR_PAGE_BUDGET = 20


class LangfuseReader:
    """Serves the contract read surface from the Langfuse query APIs."""

    def __init__(self, manager: LangfuseClientManager) -> None:
        self._m = manager

    def _request_options(self) -> dict[str, Any]:
        return {"timeout_in_seconds": self._m.read_timeout_seconds()}

    async def _active_client(self) -> Any:
        # First use may construct the SDK clients — off the event loop.
        return await asyncio.to_thread(self._m.active_client)

    async def query_metrics(self, filter: MetricsFilter) -> MetricsResult:
        client = await self._active_client()
        source = self._m.active_source()

        dimension_fields = list(filter.dimensions)
        query: dict[str, Any] = {
            "view": filter.view.value if hasattr(filter.view, "value") else filter.view,
            "metrics": [self._metric_entry(m) for m in filter.metrics],
            "dimensions": [{"field": d} for d in dimension_fields],
            # Scope every metrics query to our data via the environment marker.
            "filters": [*filter.filters, _environment_clause(source)],
            "fromTimestamp": filter.from_timestamp.isoformat(),
            "toTimestamp": filter.to_timestamp.isoformat(),
        }
        if filter.granularity:
            query["timeDimension"] = {"granularity": filter.granularity}
        if filter.order_by:
            query["orderBy"] = filter.order_by

        response = await asyncio.to_thread(
            partial(
                client.api.legacy.metrics_v1.metrics,
                query=json.dumps(query),
                request_options=self._request_options(),
            )
        )
        raw_rows: list[dict[str, Any]] = list(getattr(response, "data", []) or [])

        rows: list[MetricsRow] = []
        for raw in raw_rows:
            dims = {field: raw.get(field) for field in dimension_fields}
            metrics = {k: v for k, v in raw.items() if k not in dims}
            rows.append(MetricsRow(dimensions=dims, metrics=metrics))

        derived: dict[str, Any] = {}
        # Distinct-tag count is a cardinality, not a native metric: it is the
        # number of returned tag-groups when grouping on the tags dimension.
        if "tags" in dimension_fields:
            derived["distinct_tag_count"] = len(rows)

        return MetricsResult(rows=rows, derived=derived)

    @staticmethod
    def _metric_entry(measure: str) -> dict[str, Any]:
        return {"measure": measure, "aggregation": _DEFAULT_AGGREGATION.get(measure, "sum")}

    # --- span-window reads ----------------------------------------------------

    async def list_spans_in_window(
        self,
        t0: datetime,
        t1: datetime,
        *,
        run: str | None = None,
        kind: SpanKind | None = None,
        filter: MonitoringFilter | None = None,
        order_by: OrderBy | None = None,
    ) -> list[SpanWindowItem]:
        client = await self._active_client()
        source = self._m.active_source()

        type_filter = _KIND_TO_TYPE.get(kind) if kind is not None else None
        advanced = _observation_advanced_filter(filter)
        filter_json = json.dumps(advanced) if advanced else None

        observations = await self._fetch_observations(
            client,
            t0,
            t1,
            run=run,
            type_filter=type_filter,
            filter_json=filter_json,
            environment=source,
            name=filter.name if filter else None,
            user_id=filter.user_id if filter else None,
            level=_level_value(filter.level if filter else None),
        )

        # session_id has no native get_many param. Resolve the session to its
        # trace-id set and filter the RAW observations before mapping —
        # SpanWindowItem drops trace_id, so it can't be done after.
        if filter and filter.session_id:
            trace_ids = await self._session_trace_ids(client, filter.session_id, source)
            observations = [obs for obs in observations if obs.trace_id in trace_ids]

        selected = [obs for obs in observations if self._is_tool_granularity(obs, kind)]

        trace_tags = await self._resolve_trace_tags(client, {obs.trace_id for obs in selected if obs.trace_id})

        items = [self._to_window_item(obs, trace_tags) for obs in selected]
        return _sort_window_items(items, order_by)

    async def _fetch_observations(
        self,
        client: Any,
        t0: datetime,
        t1: datetime,
        *,
        run: str | None,
        type_filter: str | None,
        filter_json: str | None,
        environment: str,
        name: str | None,
        user_id: str | None,
        level: str | None,
    ) -> list[Any]:
        results: list[Any] = []
        page = 1
        while True:
            response = await asyncio.to_thread(
                partial(
                    client.api.legacy.observations_v1.get_many,
                    from_start_time=t0,
                    to_start_time=t1,
                    trace_id=run,
                    type=type_filter,
                    name=name,
                    user_id=user_id,
                    level=level,
                    environment=environment,
                    filter=filter_json,
                    limit=_PAGE_SIZE,
                    page=page,
                    request_options=self._request_options(),
                )
            )
            results.extend(response.data or [])
            meta = getattr(response, "meta", None)
            total_pages = getattr(meta, "total_pages", None) if meta else None
            if not total_pages or page >= total_pages:
                break
            page += 1
        return results

    async def _session_trace_ids(self, client: Any, session_id: str, source: str) -> set[str]:
        """The (source-scoped) trace-id set for a session, drained across pages."""
        ids: set[str] = set()
        page = 1
        while True:
            response = await asyncio.to_thread(
                partial(
                    client.api.trace.list,
                    session_id=session_id,
                    environment=source,
                    limit=_PAGE_SIZE,
                    page=page,
                    request_options=self._request_options(),
                )
            )
            ids.update(s.id for s in (response.data or []))
            meta = getattr(response, "meta", None)
            total_pages = getattr(meta, "total_pages", None) if meta else None
            if not total_pages or page >= total_pages:
                break
            page += 1
        return ids

    @staticmethod
    def _type_str(raw: Any) -> str:
        return (raw.value if hasattr(raw, "value") else str(raw or "")).upper()

    @classmethod
    def _is_tool_granularity(cls, obs: Any, kind: SpanKind | None) -> bool:
        """One item per tool/node execution.

        Keeps SPAN/TOOL-type node/tool observations; drops generations, events,
        the trace wrapper, grouping chains (``tools`` / ``model``), and jq
        sub-steps (``<expr_type>:<name>``). ``kind`` narrows within this set.
        """
        obs_type = cls._type_str(obs.type)
        if obs_type in _EXCLUDED_TYPES:
            return False
        if kind is not None and obs_type != _KIND_TO_TYPE.get(kind, obs_type):
            return False
        name = obs.name or ""
        if name in _GROUPING_NAMES:
            return False
        if ":" in name:
            # jq <expr_type>:<name> sub-steps are not tool-granularity steps.
            return False
        return obs_type in {"SPAN", "TOOL", "AGENT", "CHAIN", "RETRIEVER"}

    async def _resolve_trace_tags(self, client: Any, trace_ids: set[str]) -> dict[str, list[str]]:
        """Tags per trace, resolved from the parent trace (the observation row
        carries none), fetching each distinct trace once.

        A failed tag fetch is logged and degrades to an empty tag list; the span
        itself stays in the result.
        """
        trace_tags: dict[str, list[str]] = {}
        for trace_id in trace_ids:
            try:
                trace = await asyncio.to_thread(
                    partial(client.api.trace.get, trace_id, request_options=self._request_options())
                )
                trace_tags[trace_id] = list(trace.tags or [])
            except Exception:
                logger.exception("failed to fetch tags for trace %s", trace_id)
                trace_tags[trace_id] = []
        return trace_tags

    @staticmethod
    def _to_window_item(obs: Any, trace_tags: dict[str, list[str]]) -> SpanWindowItem:
        return SpanWindowItem(
            id=obs.id,
            parent_id=obs.parent_observation_id,
            name=obs.name,
            tags=trace_tags.get(obs.trace_id, []),
            input=obs.input,
            output=obs.output,
            metadata=obs.metadata,
            start=obs.start_time,
            end=obs.end_time,
        )

    # --- complete-trace reads (replay / evaluation / normalization) -----------

    async def get_trace(self, trace_id: str) -> MonitoringTrace:
        """Fetch one complete trace; never returns ``None``.

        An absent trace raises ``TraceNotFoundError`` (translated from the vendor
        error). Any other failure propagates as-is, never mapped to "not found".
        """
        client = await self._active_client()
        try:
            trace = await asyncio.to_thread(
                partial(client.api.trace.get, trace_id, request_options=self._request_options())
            )
        except NotFoundError as e:
            raise TraceNotFoundError(f"trace {trace_id} not found") from e
        return self._map_trace(trace.model_dump())

    async def list_traces(
        self,
        *,
        from_timestamp: datetime | None = None,
        to_timestamp: datetime | None = None,
        limit: int | None = None,
        page: int | None = None,
        filter: MonitoringFilter | None = None,
        order_by: OrderBy | None = None,
    ) -> list[MonitoringTraceSummary]:
        """Run SUMMARIES for one page — never per-trace bodies.

        A page costs ≤3 backend calls (native sort) / ≤3 + a bounded trace.list
        walk (metric sort): the list surface for the rows, one metrics query for
        the page's token totals, and one paged observations query for the page's
        error status. ``get_trace`` remains the only body door.
        """
        client = await self._active_client()
        source = self._m.active_source()

        kind, payload = _trace_sort(order_by)
        if kind == "metric":
            rows = await self._metric_sorted_rows(
                client,
                source,
                payload,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                limit=limit,
                page=page,
                filter=filter,
            )
        else:
            rows = await self._native_rows(
                client,
                source,
                payload,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                limit=limit,
                page=page,
                filter=filter,
            )
        if not rows:
            return []
        return await self._summarize(client, source, rows, filter)

    async def _native_rows(
        self,
        client: Any,
        source: str,
        native_sort: str,
        *,
        from_timestamp: datetime | None,
        to_timestamp: datetime | None,
        limit: int | None,
        page: int | None,
        filter: MonitoringFilter | None,
    ) -> list[Any]:
        """The page's summary rows via one server-side trace.list (native sort)."""
        return await self._list_page(
            client,
            source,
            order_by=native_sort,
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            limit=limit,
            page=page,
            filter=filter,
        )

    async def _list_page(
        self,
        client: Any,
        source: str,
        *,
        order_by: str,
        from_timestamp: datetime | None,
        to_timestamp: datetime | None,
        limit: int | None,
        page: int | None,
        filter: MonitoringFilter | None,
    ) -> list[Any]:
        """One trace.list call, returning its ``TraceWithDetails`` summary rows
        with the core/io/metrics field groups (no observation bodies)."""
        advanced = _trace_advanced_filter(filter)
        # Langfuse's advanced ``filter`` JSON overrides the native
        # fromTimestamp/toTimestamp params, so time bounds must ride in the JSON too.
        if advanced:
            advanced = _timestamp_clauses(from_timestamp, to_timestamp) + advanced
        filter_json = json.dumps(advanced) if advanced else None
        summaries = await asyncio.to_thread(
            partial(
                client.api.trace.list,
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                limit=limit,
                page=page,
                order_by=order_by,
                environment=source,
                fields=_LIST_FIELDS,
                name=filter.name if filter else None,
                user_id=filter.user_id if filter else None,
                session_id=filter.session_id if filter else None,
                version=filter.version if filter else None,
                tags=(filter.tags or None) if filter else None,
                filter=filter_json,
                request_options=self._request_options(),
            )
        )
        return list(summaries.data or [])

    async def _metric_sorted_rows(
        self,
        client: Any,
        source: str,
        measure_dir: tuple[str, str],
        *,
        from_timestamp: datetime | None,
        to_timestamp: datetime | None,
        limit: int | None,
        page: int | None,
        filter: MonitoringFilter | None,
    ) -> list[Any]:
        """The page's rows for a metric sort: globally rank the ids via the
        metrics API, then walk trace.list (newest-first) over the same window to
        collect their summary rows, in rank order. Never a per-id trace.get.

        The ranked ids can sit anywhere in the window, so the walk is bounded by
        ``_METRIC_LIST_PAGE_BUDGET`` pages; a page whose ranked ids are not all
        found within that budget raises loudly rather than returning a short row
        set that reads as a complete page.
        """
        ranked_ids = await self._metric_ranked_ids(
            client,
            source,
            measure_dir,
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            limit=limit,
            page=page,
            filter=filter,
        )
        if not ranked_ids:
            return []

        wanted = set(ranked_ids)
        found: dict[str, Any] = {}
        list_page = 1
        while wanted - found.keys():
            if list_page > _METRIC_LIST_PAGE_BUDGET:
                raise MonitoringReadNotSupportedError(
                    "metric-sorted listing could not resolve the page within the bounded window; narrow the time range"
                )
            batch = await self._list_page(
                client,
                source,
                order_by="timestamp.desc",
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                limit=_PAGE_SIZE,
                page=list_page,
                filter=filter,
            )
            for row in batch:
                if row.id in wanted:
                    found[row.id] = row
            if len(batch) < _PAGE_SIZE:
                break
            list_page += 1

        missing = wanted - found.keys()
        if missing:
            raise MonitoringReadNotSupportedError(
                "metric-sorted listing could not locate every ranked row within the bounded window; "
                "narrow the time range"
            )
        return [found[trace_id] for trace_id in ranked_ids]

    async def _metric_ranked_ids(
        self,
        client: Any,
        source: str,
        measure_dir: tuple[str, str],
        *,
        from_timestamp: datetime | None,
        to_timestamp: datetime | None,
        limit: int | None,
        page: int | None,
        filter: MonitoringFilter | None,
    ) -> list[str]:
        """Globally rank trace-ids by an aggregated measure (cost / latency /
        tokens) via the metrics API, which can order by it where ``trace.list``
        cannot. Returns the page slice of the top-N in rank order.

        Bad inputs (no time bound, non-positive limit/page, paging past the row
        cap) raise before any request.
        """
        measure, direction = measure_dir
        if from_timestamp is None:
            raise MonitoringReadNotSupportedError("metric sort requires from_timestamp")
        if limit is None or limit < 1:
            raise MonitoringReadNotSupportedError("metric sort requires limit >= 1")
        if page is not None and page < 1:
            raise MonitoringReadNotSupportedError("metric sort requires page >= 1 (1-based)")

        # config.row_limit is a server-enforced top-N cap (not an offset), so the
        # query must reach the whole prefix up to the requested page's end.
        reach = ((page or 1) - 1) * limit + limit
        if reach > _METRIC_ROW_LIMIT_MAX:
            raise MonitoringReadNotSupportedError(
                f"metric sort paging beyond the {_METRIC_ROW_LIMIT_MAX}-row metrics cap (reach {reach})"
            )

        query: dict[str, Any] = {
            "view": "traces",
            "metrics": [{"measure": measure, "aggregation": "sum"}],
            "dimensions": [{"field": "id"}],
            "filters": [_environment_clause(source), *_trace_metric_filter(filter)],
            # The orderBy key the server emits for a summed measure is sum_<measure>.
            "orderBy": [{"field": f"sum_{measure}", "direction": direction}],
            "fromTimestamp": from_timestamp.isoformat(),
            "toTimestamp": (to_timestamp or datetime.now(UTC)).isoformat(),
            # Nested config wrapper; a flat row_limit is ignored.
            "config": {"row_limit": reach},
        }
        # Stay on legacy.metrics_v1 view="traces": the v2 metrics endpoint has no
        # traces view and rejects high-cardinality id grouping.
        response = await asyncio.to_thread(
            partial(
                client.api.legacy.metrics_v1.metrics,
                query=json.dumps(query),
                request_options=self._request_options(),
            )
        )
        rows = list(getattr(response, "data", []) or [])
        # row_limit is a cap, not an offset; slice this page out of the ranked rows.
        start = ((page or 1) - 1) * limit
        return [row["id"] for row in rows][start : start + limit]

    async def _summarize(
        self, client: Any, source: str, rows: list[Any], filter: MonitoringFilter | None
    ) -> list[MonitoringTraceSummary]:
        """Build the page's summaries from the list rows plus two batched, window-
        scoped enrichments: token totals (one metrics query) and error status (one
        paged observations query). Order is preserved."""
        timestamps = [row.timestamp for row in rows if row.timestamp is not None]
        if not timestamps:
            return [self._to_summary(row, {}, set()) for row in rows]

        # Token window is keyed on trace timestamp (the metrics traces view filters
        # on it); the status window is keyed on OBSERVATION start time, which lags
        # the trace timestamp by up to the trace's own latency — so its upper bound
        # follows the latest estimated trace end, not just the newest timestamp.
        t_min = min(timestamps)
        t_max = max(timestamps)
        page_ids = [row.id for row in rows]
        estimated_ends = [
            row.timestamp + timedelta(seconds=row.latency)
            for row in rows
            if row.timestamp is not None and _nonneg_number(getattr(row, "latency", None))
        ]
        # A row with no latency is still in flight: its observations can extend to
        # the present, so the error window's upper bound must reach now.
        if any(not _nonneg_number(getattr(row, "latency", None)) for row in rows):
            estimated_ends.append(datetime.now(UTC))
        obs_end = (max(estimated_ends) if estimated_ends else t_max) + _WINDOW_EPSILON

        tokens_by_id, error_ids = await asyncio.gather(
            self._tokens_for_window(client, source, t_min, t_max + _WINDOW_EPSILON, page_ids, filter),
            self._error_trace_ids(client, source, t_min, obs_end),
        )
        return [self._to_summary(row, tokens_by_id, error_ids) for row in rows]

    async def _tokens_for_window(
        self, client: Any, source: str, t0: datetime, t1: datetime, page_ids: list[str], filter: MonitoringFilter | None
    ) -> dict[str, float]:
        """Per-trace summed token totals over the page window via ONE metrics
        traces-view query (dimension id, measure totalTokens). The page filter's
        view-supported clauses narrow the metrics population; correctness comes
        from the per-id join, so the unsupported clauses are dropped, not raised.
        A trace absent from the result carries no usage.

        The metrics view caps at ``_METRIC_ROW_LIMIT_MAX`` rows: if the cap is hit
        AND a page id is uncovered, the token join cannot tell "no usage" from
        "truncated" — it raises loudly rather than reporting a silent None. If rows
        come back yet none carry a recognised token measure, the query shape is
        wrong — it raises rather than reporting every trace as usage-less."""
        query: dict[str, Any] = {
            "view": "traces",
            "metrics": [{"measure": "totalTokens", "aggregation": "sum"}],
            "dimensions": [{"field": "id"}],
            "filters": [_environment_clause(source), *_metric_population_filter(filter)],
            "fromTimestamp": t0.isoformat(),
            "toTimestamp": t1.isoformat(),
            "config": {"row_limit": _METRIC_ROW_LIMIT_MAX},
        }
        response = await asyncio.to_thread(
            partial(
                client.api.legacy.metrics_v1.metrics,
                query=json.dumps(query),
                request_options=self._request_options(),
            )
        )
        rows = list(getattr(response, "data", []) or [])
        tokens_by_id: dict[str, float] = {}
        # A trace whose summed measure is null IS covered by the result (no usage),
        # so coverage is the set of ids carrying the measure column, not just those
        # with a non-null value — otherwise a null-sum page id reads as truncated.
        covered_ids: set[str] = set()
        for raw in rows:
            key = _measure_key(raw, "totalTokens")
            if key is None:
                continue
            trace_id = raw.get("id")
            if trace_id is None:
                continue
            covered_ids.add(trace_id)
            value = _measure_value(raw[key])
            if value is not None:
                tokens_by_id[trace_id] = value
        if rows and not covered_ids:
            raise MonitoringReadNotSupportedError("token totals came back without a recognised total-tokens measure")
        if len(rows) >= _METRIC_ROW_LIMIT_MAX:
            uncovered = [tid for tid in page_ids if tid not in covered_ids]
            if uncovered:
                raise MonitoringReadNotSupportedError(
                    "token totals could not be resolved within the bounded window; narrow the time range"
                )
        return tokens_by_id

    async def _error_trace_ids(self, client: Any, source: str, t0: datetime, t1: datetime) -> set[str]:
        """The (source-scoped) trace ids with an ERROR-level observation whose
        start falls in the window, drained across pages up to ``_ERROR_PAGE_BUDGET``.

        Errors are rare, so the window normally resolves in one call. Exceeding the
        budget, or a full page returned without a page count to bound the walk,
        raises loudly — it never stops quietly on a partial result."""
        ids: set[str] = set()
        page = 1
        while True:
            response = await asyncio.to_thread(
                partial(
                    client.api.legacy.observations_v1.get_many,
                    from_start_time=t0,
                    to_start_time=t1,
                    level="ERROR",
                    environment=source,
                    limit=_PAGE_SIZE,
                    page=page,
                    request_options=self._request_options(),
                )
            )
            data = response.data or []
            for obs in data:
                if obs.trace_id:
                    ids.add(obs.trace_id)
            meta = getattr(response, "meta", None)
            total_pages = getattr(meta, "total_pages", None) if meta else None
            if total_pages is None:
                if len(data) >= _PAGE_SIZE:
                    raise MonitoringReadNotSupportedError(
                        "error status could not be resolved within the bounded window; narrow the time range"
                    )
                break
            if page >= total_pages:
                break
            if page >= _ERROR_PAGE_BUDGET:
                raise MonitoringReadNotSupportedError(
                    "error status could not be resolved within the bounded window; narrow the time range"
                )
            page += 1
        return ids

    @staticmethod
    def _to_summary(row: Any, tokens_by_id: dict[str, float], error_ids: set[str]) -> MonitoringTraceSummary:
        latency = getattr(row, "latency", None)
        latency_ms = latency * 1000.0 if _nonneg_number(latency) else None
        tokens = tokens_by_id.get(row.id)
        cost = getattr(row, "total_cost", None)
        return MonitoringTraceSummary(
            id=row.id,
            timestamp=row.timestamp,
            name=getattr(row, "name", None),
            tags=list(getattr(row, "tags", None) or []),
            input_preview=preview(getattr(row, "input", None)),
            output_preview=preview(getattr(row, "output", None)),
            latency_ms=latency_ms,
            # trace.list with the metrics field group returns real costs; -1 marks
            # a backend that could not compute one — surface it as None, not -1.
            total_cost=cost if _nonneg_number(cost) else None,
            total_tokens=int(tokens) if tokens is not None else None,
            status="error" if row.id in error_ids else "ok",
        )

    @classmethod
    def _map_trace(cls, raw: dict[str, Any]) -> MonitoringTrace:
        observations = [cls._map_observation(o) for o in (raw.get("observations") or [])]
        return MonitoringTrace(
            id=raw.get("id", ""),
            timestamp=raw.get("timestamp"),
            tags=list(raw.get("tags") or []),
            input=raw.get("input"),
            output=raw.get("output"),
            metadata=raw.get("metadata"),
            # _pick preserves a genuine 0.0 cost (falsy-but-present), not "missing".
            total_cost=_pick(raw, "total_cost", "totalCost"),
            observations=observations,
        )

    @staticmethod
    def _map_observation(raw: dict[str, Any]) -> MonitoringObservation:
        return MonitoringObservation(
            id=raw.get("id", ""),
            trace_id=_pick(raw, "trace_id", "traceId"),
            parent_id=_pick(raw, "parent_observation_id", "parentObservationId"),
            type=raw.get("type"),
            name=raw.get("name"),
            level=raw.get("level"),
            status_message=_pick(raw, "status_message", "statusMessage"),
            input=raw.get("input"),
            output=raw.get("output"),
            metadata=raw.get("metadata"),
            usage=_pick(raw, "usage_details", "usageDetails", "usage"),
            model=_pick(raw, "provided_model_name", "model"),
            start=_pick(raw, "start_time", "startTime"),
            end=_pick(raw, "end_time", "endTime"),
        )


# --- filter mapping ----------------------------------------------------------


def _pick(raw: dict[str, Any], *keys: str) -> Any:
    """First of ``keys`` present in ``raw`` with a non-None value, else None.

    Preserves falsy-but-valid values (``0``, ``""``, ``[]``), unlike ``a or b``.
    """
    for key in keys:
        value = raw.get(key)
        if value is not None:
            return value
    return None


def _nonneg_number(value: Any) -> TypeGuard[float]:
    """A real, non-negative number — the -1 sentinel a backend returns for an
    uncomputed metric is excluded."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _measure_key(row: dict[str, Any], measure: str) -> str | None:
    """The row's column for a summed measure. The backend names the output column
    by measure + aggregation (e.g. ``sum_totalTokens`` / ``totalTokens_sum``) and
    the exact key varies, so match by substring with an exact-key fast path.
    ``None`` when no column matches — presence is decided by the column, never by
    its value (a null sum is a real measure cell, not a missing column)."""
    if measure in row:
        return measure
    needle = measure.lower()
    for key in row:
        if isinstance(key, str) and needle in key.lower():
            return key
    return None


def _measure_value(cell: Any) -> float | None:
    """Parse a metrics measure cell to a non-negative finite float. Numbers and
    numeric strings (ClickHouse-backed sums serialise as strings) become floats;
    ``None`` and the empty string mean no usage and become ``None``. A bool, an
    unparseable string, or a non-finite/negative value is not a token sum —
    ``None`` (a token sum is a non-negative finite number)."""
    if isinstance(cell, bool):
        return None
    if isinstance(cell, (int, float)):
        number = float(cell)
    elif isinstance(cell, str):
        text = cell.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _environment_clause(source: str) -> dict[str, Any]:
    return {"column": "environment", "operator": "=", "value": source, "type": "string"}


def _level_value(level: Any) -> str | None:
    if level is None:
        return None
    return level.value if hasattr(level, "value") else str(level)


def _number_clauses(column: str, low: float | None, high: float | None) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    if low is not None:
        clauses.append({"column": column, "operator": ">=", "value": low, "type": "number"})
    if high is not None:
        clauses.append({"column": column, "operator": "<=", "value": high, "type": "number"})
    return clauses


def _metadata_clauses(metadata: dict[str, str]) -> list[dict[str, Any]]:
    # One stringObject clause per key; ``key`` is required for nested metadata.
    return [
        {"column": "metadata", "type": "stringObject", "key": k, "operator": "=", "value": v}
        for k, v in sorted(metadata.items())
    ]


def _shared_metric_clauses(filter: MonitoringFilter) -> list[dict[str, Any]]:
    """Advanced-filter clauses common to traces and observations: the cost /
    token / latency ranges and the metadata equalities."""
    clauses: list[dict[str, Any]] = []
    clauses += _number_clauses("totalCost", filter.min_cost, filter.max_cost)
    clauses += _number_clauses("totalTokens", filter.min_tokens, filter.max_tokens)
    clauses += _number_clauses("latency", filter.min_latency, filter.max_latency)
    clauses += _metadata_clauses(filter.metadata)
    return clauses


def _observation_advanced_filter(filter: MonitoringFilter | None) -> list[dict[str, Any]]:
    """The get_many advanced filter. ``name`` / ``user_id`` / ``level`` /
    ``environment`` ride native params; tags / model / cost / tokens / latency /
    metadata go here."""
    if filter is None:
        return []
    clauses: list[dict[str, Any]] = []
    if filter.tags:
        clauses.append({"column": "traceTags", "operator": "any of", "value": filter.tags, "type": "arrayOptions"})
    if filter.model is not None:
        clauses.append({"column": "model", "operator": "=", "value": filter.model, "type": "string"})
    clauses += _shared_metric_clauses(filter)
    return clauses


def _timestamp_clauses(t0: datetime | None, t1: datetime | None) -> list[dict[str, Any]]:
    """Trace ``timestamp`` range as advanced-filter clauses, so the time bound
    survives when other advanced clauses force the filter JSON."""
    clauses: list[dict[str, Any]] = []
    if t0 is not None:
        clauses.append({"column": "timestamp", "operator": ">=", "value": t0.isoformat(), "type": "datetime"})
    if t1 is not None:
        # Half-open [t0, t1): exclusive upper, matching trace.list's native toTimestamp.
        clauses.append({"column": "timestamp", "operator": "<", "value": t1.isoformat(), "type": "datetime"})
    return clauses


def _trace_advanced_filter(filter: MonitoringFilter | None) -> list[dict[str, Any]]:
    """The trace.list advanced filter. ``name`` / ``user_id`` / ``session_id`` /
    ``version`` / ``tags`` / ``environment`` ride native params; ``level`` plus the
    cost / token / latency / metadata clauses go here. Traces have no ``model``
    column — a model filter on traces is unsupported."""
    if filter is None:
        return []
    if filter.model is not None:
        raise MonitoringReadNotSupportedError(
            "list_traces cannot filter on 'model' (no model column on traces); filter spans instead"
        )
    clauses: list[dict[str, Any]] = []
    if filter.level is not None:
        clauses.append({"column": "level", "operator": "=", "value": _level_value(filter.level), "type": "string"})
    clauses += _shared_metric_clauses(filter)
    return clauses


def _metrics_supported_clauses(filter: MonitoringFilter) -> list[dict[str, Any]]:
    """The metrics ``traces`` view's supported filter clauses: ``name`` /
    ``userId`` / ``sessionId`` / ``tags`` (column ``tags``, NOT ``traceTags``) /
    ``metadata``."""
    clauses: list[dict[str, Any]] = []
    if filter.name is not None:
        clauses.append({"column": "name", "operator": "=", "value": filter.name, "type": "string"})
    if filter.user_id is not None:
        clauses.append({"column": "userId", "operator": "=", "value": filter.user_id, "type": "string"})
    if filter.session_id is not None:
        clauses.append({"column": "sessionId", "operator": "=", "value": filter.session_id, "type": "string"})
    if filter.tags:
        clauses.append({"column": "tags", "operator": "any of", "value": filter.tags, "type": "arrayOptions"})
    clauses += _metadata_clauses(filter.metadata)
    return clauses


def _metrics_unsupported_clauses(filter: MonitoringFilter) -> list[str]:
    """The names of ``filter`` clauses the metrics ``traces`` view has no column
    for: ``level`` / ``model`` / ``version`` and the cost / token / latency ranges.

    ``version`` is a native trace.list column (used on the timestamp-sort path) but
    the metrics ``traces`` view exposes no version dimension, so a metric SORT
    combined with a version filter raises here rather than misranking a page on a
    silently-dropped clause."""
    unsupported: list[str] = []
    if filter.level is not None:
        unsupported.append("level")
    if filter.model is not None:
        unsupported.append("model")
    if filter.version is not None:
        unsupported.append("version")
    for name, value in (
        ("min_cost", filter.min_cost),
        ("max_cost", filter.max_cost),
        ("min_tokens", filter.min_tokens),
        ("max_tokens", filter.max_tokens),
        ("min_latency", filter.min_latency),
        ("max_latency", filter.max_latency),
    ):
        if value is not None:
            unsupported.append(name)
    return unsupported


def _trace_metric_filter(filter: MonitoringFilter | None) -> list[dict[str, Any]]:
    """The metric-SORT rank filter array. The metrics ``traces`` view accepts only
    ``name`` / ``userId`` / ``sessionId`` / ``tags`` (column ``tags``, NOT
    ``traceTags``) / ``metadata``; ``level`` / ``model`` / cost / token / latency
    are unsupported and RAISE here — on the sort path the clause decides the rank,
    so a dropped clause would misrank the page.
    """
    if filter is None:
        return []
    unsupported = _metrics_unsupported_clauses(filter)
    if unsupported:
        raise MonitoringReadNotSupportedError(
            f"metric sort cannot filter on {unsupported}; the metrics traces view "
            "has no such column — sort by timestamp to use these filters"
        )
    return _metrics_supported_clauses(filter)


def _metric_population_filter(filter: MonitoringFilter | None) -> list[dict[str, Any]]:
    """The token-join filter array. It narrows the metrics POPULATION only —
    per-trace correctness comes from joining the result by trace id, not from this
    filter — so the clauses the metrics ``traces`` view has no column for
    (``level`` / ``model`` and the cost / token / latency ranges) are DROPPED here
    rather than raised, unlike the metric-SORT path. Keeps the view's supported
    clauses: ``name`` / ``userId`` / ``sessionId`` / ``tags`` / ``metadata``."""
    if filter is None:
        return []
    return _metrics_supported_clauses(filter)


# --- sorting -------------------------------------------------------------------


def _trace_sort(order_by: OrderBy | None) -> tuple[str, Any]:
    """Resolve a trace sort into a ``(kind, payload)`` pair the caller branches on:

    - ``("native", "<field>.<direction>")`` for ``timestamp`` / ``name`` / ``id``
      — handed to ``trace.list`` for a server-side sort.
    - ``("metric", (<measure>, <direction>))`` for ``total_cost`` / ``latency`` /
      ``total_tokens`` — no native trace sort, ranked globally via the metrics API.
    """
    if order_by is None:
        return "native", "timestamp.desc"
    if order_by.field not in _TRACE_SORT_FIELDS:
        raise MonitoringReadNotSupportedError(
            f"list_traces cannot sort on {order_by.field!r}; supported: {sorted(_TRACE_SORT_FIELDS)}"
        )
    native = _TRACE_NATIVE_SORT.get(order_by.field)
    if native is not None:
        return "native", f"{native}.{order_by.direction}"
    return "metric", (_TRACE_METRIC_MEASURE[order_by.field], order_by.direction)


def _sort_window_items(items: list[SpanWindowItem], order_by: OrderBy | None) -> list[SpanWindowItem]:
    field = "start" if order_by is None else order_by.field
    direction = "desc" if order_by is None else order_by.direction
    if field not in _SPAN_SORT_FIELDS:
        raise MonitoringReadNotSupportedError(
            f"list_spans_in_window cannot sort on {field!r}; supported: {sorted(_SPAN_SORT_FIELDS)}"
        )

    def _key(item: SpanWindowItem) -> Any:
        if field == "duration":
            if item.start is None or item.end is None:
                return None
            return (item.end - item.start).total_seconds()
        return getattr(item, field)

    return _none_last_sorted(items, _key, direction == "desc")


def _none_last_sorted(items: list[Any], key: Callable[[Any], Any], reverse: bool) -> list[Any]:
    """Sort by ``key`` with ``None``-keyed items always last, so a ``None`` never
    participates in the comparison."""
    present = [it for it in items if key(it) is not None]
    missing = [it for it in items if key(it) is None]
    present.sort(key=key, reverse=reverse)
    return present + missing
