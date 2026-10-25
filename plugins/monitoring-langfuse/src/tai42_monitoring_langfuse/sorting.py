"""Sort resolution for the run list and the span window."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_contract.monitoring import MonitoringReadNotServedError, OrderBy, SpanWindowItem

TRACE_SORT_FIELDS: frozenset[str] = frozenset({"timestamp", "total_cost", "name", "id", "latency", "total_tokens"})
# The sorts a ranked top-N metrics query serves, with the measure it sums.
RANKED_MEASURE: dict[str, str] = {"total_cost": "totalCost", "total_tokens": "totalTokens"}
_SPAN_SORT_FIELDS = frozenset({"start", "end", "duration", "name", "id"})


def resolve_trace_sort(order_by: OrderBy | None) -> OrderBy:
    """The run-list sort, newest first when none is given; a field outside the declaration is not served."""
    if order_by is None:
        return OrderBy(field="timestamp", direction="desc")
    if order_by.field not in TRACE_SORT_FIELDS:
        raise MonitoringReadNotServedError(
            f"list_traces cannot sort on {order_by.field!r}; supported: {sorted(TRACE_SORT_FIELDS)}"
        )
    return order_by


def _sort_window_items(items: list[SpanWindowItem], order_by: OrderBy | None) -> list[SpanWindowItem]:
    field = "start" if order_by is None else order_by.field
    direction = "desc" if order_by is None else order_by.direction
    if field not in _SPAN_SORT_FIELDS:
        raise MonitoringReadNotServedError(
            f"list_spans_in_window cannot sort on {field!r}; supported: {sorted(_SPAN_SORT_FIELDS)}"
        )

    def _key(item: SpanWindowItem) -> Any:
        if field == "duration":
            if item.end is None:
                return None
            return (item.end - item.start).total_seconds()
        return getattr(item, field)

    return _none_last_sorted(items, _key, direction == "desc")


def _none_last_sorted(items: list[Any], key: Callable[[Any], Any], reverse: bool) -> list[Any]:
    """Sort by ``key`` with ``None``-keyed items always last, so a ``None`` never participates in comparison."""
    present = [it for it in items if key(it) is not None]
    missing = [it for it in items if key(it) is None]
    present.sort(key=key, reverse=reverse)
    return present + missing
