"""The Traces API query surface: ``get_trace`` (one complete trace) and ``list_traces`` (a page of summaries).

``list_traces`` pages summaries native or metric-ranked, with token and
error-status enrichment.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from langfuse.api.commons.errors import NotFoundError
from tai42_contract.monitoring import (
    MonitoringFilter,
    MonitoringObservation,
    MonitoringReadNotSupportedError,
    MonitoringTrace,
    MonitoringTraceSummary,
    OrderBy,
    TraceNotFoundError,
    preview,
)

from tai42_monitoring_langfuse.filters import (
    _environment_clause,
    _metric_population_filter,
    _timestamp_clauses,
    _trace_advanced_filter,
    _trace_metric_filter,
)
from tai42_monitoring_langfuse.measures import _measure_key, _measure_value, _nonneg_number, _pick
from tai42_monitoring_langfuse.query_base import _PAGE_SIZE, _LangfuseQuery
from tai42_monitoring_langfuse.sorting import _trace_sort

# The trace.list field groups a row summary needs: core attributes, io (input,
# output, metadata) and metrics (latency, total_cost). Observation bodies are
# never listed — get_trace is the only body door.
_LIST_FIELDS = "core,io,metrics"
# Buffer added to a page's newest timestamp so a trace/observation exactly at the
# upper bound is caught by the backend's exclusive upper bound.
_WINDOW_EPSILON = timedelta(seconds=1)
# The metrics traces view server-enforces config.row_limit in [1, 1000].
_METRIC_ROW_LIMIT_MAX = 1000
# Max trace.list pages walked to cover a metric-sorted page's ranked ids before
# raising loudly — the ranked ids scatter across the whole window, so a wide
# window with the ranked traces at its far end could otherwise page unbounded.
_METRIC_LIST_PAGE_BUDGET = 20
# Max observation pages walked to collect a page's error ids before raising
# loudly — errors are rare so the window normally resolves in one call; a wide
# window with many errors could otherwise page unbounded.
_ERROR_PAGE_BUDGET = 20


class TraceQuery(_LangfuseQuery):
    """Serves ``get_trace`` and ``list_traces`` from the Langfuse Traces API."""

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
        filter_: MonitoringFilter | None = None,
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
                filter_=filter_,
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
                filter_=filter_,
            )
        if not rows:
            return []
        return await self._summarize(client, source, rows, filter_)

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
        filter_: MonitoringFilter | None,
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
            filter_=filter_,
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
        filter_: MonitoringFilter | None,
    ) -> list[Any]:
        """One trace.list call, returning its ``TraceWithDetails`` summary rows.

        Rows carry the core/io/metrics field groups (no observation bodies).
        """
        advanced = _trace_advanced_filter(filter_)
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
                name=filter_.name if filter_ else None,
                user_id=filter_.user_id if filter_ else None,
                session_id=filter_.session_id if filter_ else None,
                version=filter_.version if filter_ else None,
                tags=(filter_.tags or None) if filter_ else None,
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
        filter_: MonitoringFilter | None,
    ) -> list[Any]:
        """The page's rows for a metric sort, in rank order.

        Globally ranks the ids via the metrics API, then walks trace.list
        (newest-first) over the same window to collect their summary rows. Never a
        per-id trace.get. The ranked ids can sit anywhere in the window, so the walk is bounded by
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
            filter_=filter_,
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
                filter_=filter_,
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
        filter_: MonitoringFilter | None,
    ) -> list[str]:
        """Globally rank trace-ids by an aggregated measure (cost / latency / tokens) via the metrics API.

        The metrics API can order by the measure where ``trace.list`` cannot.
        Returns the page slice of the top-N in rank order. Bad inputs (no time
        bound, non-positive limit/page, paging past the row
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
            "filters": [_environment_clause(source), *_trace_metric_filter(filter_)],
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
        self, client: Any, source: str, rows: list[Any], filter_: MonitoringFilter | None
    ) -> list[MonitoringTraceSummary]:
        """Build the page's summaries from the list rows plus two batched, window-scoped enrichments.

        The enrichments are token totals (one metrics query) and error status (one
        paged observations query). Order is preserved.
        """
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
            self._tokens_for_window(client, source, t_min, t_max + _WINDOW_EPSILON, page_ids, filter_),
            self._error_trace_ids(client, source, t_min, obs_end),
        )
        return [self._to_summary(row, tokens_by_id, error_ids) for row in rows]

    async def _tokens_for_window(
        self,
        client: Any,
        source: str,
        t0: datetime,
        t1: datetime,
        page_ids: list[str],
        filter_: MonitoringFilter | None,
    ) -> dict[str, float]:
        """Per-trace summed token totals over the page window via ONE metrics traces-view query.

        Uses dimension id, measure totalTokens. The page filter's view-supported
        clauses narrow the metrics population; correctness comes from the per-id
        join, so the unsupported clauses are dropped, not raised. A trace absent
        from the result carries no usage.

        The metrics view caps at ``_METRIC_ROW_LIMIT_MAX`` rows: if the cap is hit
        AND a page id is uncovered, the token join cannot tell "no usage" from
        "truncated" — it raises loudly rather than reporting a silent None. If rows
        come back yet none carry a recognised token measure, the query shape is
        wrong — it raises rather than reporting every trace as usage-less.
        """
        query: dict[str, Any] = {
            "view": "traces",
            "metrics": [{"measure": "totalTokens", "aggregation": "sum"}],
            "dimensions": [{"field": "id"}],
            "filters": [_environment_clause(source), *_metric_population_filter(filter_)],
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
        """The (source-scoped) trace ids with an ERROR-level observation whose start falls in the window.

        Drained across pages up to ``_ERROR_PAGE_BUDGET``. Errors are rare, so the
        window normally resolves in one call. Exceeding the
        budget, or a full page returned without a page count to bound the walk,
        raises loudly — it never stops quietly on a partial result.
        """
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
