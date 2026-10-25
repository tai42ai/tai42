"""The ``list_spans_in_window`` read: one item per tool or node run, from the observations list."""

from __future__ import annotations

from datetime import datetime

from tai42_contract.monitoring import (
    STEP_ROLE_METADATA_KEY,
    MonitoringFilter,
    OrderBy,
    SpanKind,
    SpanWindowItem,
    StepRole,
)

from tai42_monitoring_langfuse.filters import environment_clause, span_clauses, string_clause, window_clauses
from tai42_monitoring_langfuse.observation_query import (
    ObservationQuery,
    ObservationRow,
    decode_io,
    observation_kind,
    producer_metadata,
)
from tai42_monitoring_langfuse.sorting import _sort_window_items

# The Langfuse observation types a step can be; generations, embeddings and events never are.
_STEP_TYPES = ("AGENT", "CHAIN", "RETRIEVER", "SPAN", "TOOL")
# The producer step-role markers that make a record part of the outline but not a step.
_NOT_A_STEP = frozenset({StepRole.GROUPING.value, StepRole.SUB_STEP.value})
_WINDOW_FIELDS = "core,basic,io,metadata,trace_context"


class SpanWindowQuery(ObservationQuery):
    """Serves ``list_spans_in_window`` from the Langfuse observations list."""

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
        """The step observations that started in ``[t0, t1)``, narrowed by ``run``, ``kind`` and ``filter_``."""
        types = [t for t in _STEP_TYPES if kind is None or observation_kind(t) is kind]
        if not types:
            return []
        client = await self._active_client()
        clauses = [
            environment_clause(self._m.active_source()),
            *window_clauses(t0, t1),
            {"column": "type", "operator": "any of", "value": types, "type": "stringOptions"},
        ]
        if run is not None:
            clauses.append(string_clause("traceId", run))
        clauses += span_clauses(filter_)
        rows = await self._walk_all(client, fields=_WINDOW_FIELDS, clauses=clauses, expand_metadata=True)
        items = [self._to_window_item(row) for row in rows if self._is_step(row)]
        return _sort_window_items(items, order_by)

    @staticmethod
    def _is_step(row: ObservationRow) -> bool:
        """Whether a step-typed observation is a step: its producer metadata marks it no grouping or sub-step."""
        metadata = producer_metadata(row.metadata, row.id) or {}
        return metadata.get(STEP_ROLE_METADATA_KEY) not in _NOT_A_STEP

    @staticmethod
    def _to_window_item(row: ObservationRow) -> SpanWindowItem:
        return SpanWindowItem(
            id=row.id,
            parent_id=row.parent_observation_id,
            name=row.name,
            tags=list(row.tags or []),
            input=decode_io(row.input),
            output=decode_io(row.output),
            metadata=row.metadata,
            start=row.start_time,
            end=row.end_time,
        )
