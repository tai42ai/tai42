"""The Observations API query surface for ``list_spans_in_window``.

Returns one item per tool/node run, tag-enriched and client-sorted.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from functools import partial
from typing import Any

from tai42_contract.monitoring import (
    MonitoringFilter,
    OrderBy,
    SpanKind,
    SpanWindowItem,
)

from tai42_monitoring_langfuse.filters import _level_value, _observation_advanced_filter
from tai42_monitoring_langfuse.query_base import _PAGE_SIZE, _LangfuseQuery
from tai42_monitoring_langfuse.sorting import _sort_window_items

logger = logging.getLogger(__name__)

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

        type_filter = _KIND_TO_TYPE.get(kind) if kind is not None else None
        advanced = _observation_advanced_filter(filter_)
        filter_json = json.dumps(advanced) if advanced else None

        observations = await self._fetch_observations(
            client,
            t0,
            t1,
            run=run,
            type_filter=type_filter,
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
        """Tags per trace, resolved from the parent trace, fetching each distinct trace once.

        The observation row carries no tags, so the parent trace is the source.

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
