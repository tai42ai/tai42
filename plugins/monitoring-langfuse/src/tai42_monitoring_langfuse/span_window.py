"""The Observations API query surface for ``list_spans_in_window``.

Returns one item per tool/node run, tag-enriched and client-sorted.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from tai42_contract.monitoring import (
    STEP_ROLE_METADATA_KEY,
    MonitoringFilter,
    OrderBy,
    SpanKind,
    SpanWindowItem,
    StepRole,
)

from tai42_monitoring_langfuse.filters import _level_value, _observation_advanced_filter
from tai42_monitoring_langfuse.query_base import PAGE_SIZE, _LangfuseQuery
from tai42_monitoring_langfuse.sorting import _sort_window_items
from tai42_monitoring_langfuse.trace_query import observation_kind, observation_type, producer_metadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TraceTags:
    """A trace's tags with whether the fetch succeeded.

    ``available`` is ``False`` only when the tag fetch itself faulted; an empty
    ``tags`` with ``available=True`` is a genuinely untagged trace.
    """

    tags: list[str]
    available: bool


# The Langfuse observation types a step can be; generations, embeddings, events and
# the trace wrapper never are.
_STEP_TYPES = frozenset({"SPAN", "TOOL", "AGENT", "CHAIN", "RETRIEVER"})
# The producer step-role markers that make a record part of the outline but not a step.
_NOT_A_STEP = frozenset({StepRole.GROUPING.value, StepRole.SUB_STEP.value})


class SpanWindowQuery(_LangfuseQuery):
    """Serves ``list_spans_in_window`` from the Langfuse Observations API."""

    async def list_spans_in_window(
        self,
        t0: datetime,
        t1: datetime,
        *,
        run: str | None = None,
        kind: SpanKind | None = None,
        filter_: MonitoringFilter | None = None,
        order_by: OrderBy | None = None,
    ) -> list[SpanWindowItem]:
        """List tool/node spans in ``[t0, t1]``, optionally narrowed by ``run``/``kind``/``filter_`` and ordered."""
        client = await self._active_client()
        source = self._m.active_source()

        advanced = _observation_advanced_filter(filter_)
        filter_json = json.dumps(advanced) if advanced else None

        observations = await self._fetch_observations(
            client,
            t0,
            t1,
            run=run,
            filter_json=filter_json,
            environment=source,
            name=filter_.name if filter_ else None,
            user_id=filter_.user_id if filter_ else None,
            level=_level_value(filter_.level if filter_ else None),
        )

        # session_id has no native get_many param. Resolve the session to its
        # trace-id set and filter the RAW observations before mapping —
        # SpanWindowItem drops trace_id, so it can't be done after.
        if filter_ and filter_.session_id:
            trace_ids = await self._session_trace_ids(client, filter_.session_id, source)
            observations = [obs for obs in observations if obs.trace_id in trace_ids]

        # Narrowing by kind is client-side: one neutral kind spans several Langfuse types.
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
                    name=name,
                    user_id=user_id,
                    level=level,
                    environment=environment,
                    filter=filter_json,
                    limit=PAGE_SIZE,
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
                    limit=PAGE_SIZE,
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
    def _is_tool_granularity(obs: Any, kind: SpanKind | None) -> bool:
        """One item per tool/node execution, selected by type and the producers' step marker.

        Keeps SPAN/TOOL/AGENT/CHAIN/RETRIEVER observations; drops generations, embeddings,
        events, the trace wrapper, and any record whose producer metadata marks it a
        grouping or a sub-step. ``kind`` narrows within this set by the mapped neutral kind.
        """
        if observation_type(obs.type) not in _STEP_TYPES:
            return False
        if kind is not None and observation_kind(obs.type) is not kind:
            return False
        metadata = producer_metadata(obs.metadata, obs.id) or {}
        return metadata.get(STEP_ROLE_METADATA_KEY) not in _NOT_A_STEP

    async def _resolve_trace_tags(self, client: Any, trace_ids: set[str]) -> dict[str, _TraceTags]:
        """Tags per trace, resolved from the parent trace, fetching each distinct trace once.

        The observation row carries no tags, so the parent trace is the source.

        A failed tag fetch is logged and marks the trace unavailable; the span
        itself stays in the result.
        """
        trace_tags: dict[str, _TraceTags] = {}
        for trace_id in trace_ids:
            try:
                trace = await asyncio.to_thread(
                    partial(client.api.trace.get, trace_id, request_options=self._request_options())
                )
                trace_tags[trace_id] = _TraceTags(tags=list(trace.tags or []), available=True)
            except Exception:
                logger.exception("failed to fetch tags for trace %s", trace_id)
                trace_tags[trace_id] = _TraceTags(tags=[], available=False)
        return trace_tags

    @staticmethod
    def _to_window_item(obs: Any, trace_tags: dict[str, _TraceTags]) -> SpanWindowItem:
        result = trace_tags.get(obs.trace_id)
        return SpanWindowItem(
            id=obs.id,
            parent_id=obs.parent_observation_id,
            name=obs.name,
            tags=result.tags if result is not None else [],
            tags_available=result.available if result is not None else True,
            input=obs.input,
            output=obs.output,
            metadata=obs.metadata,
            start=obs.start_time,
            end=obs.end_time,
        )
