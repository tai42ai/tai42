"""Sort resolution for the trace and span-window read surfaces.

Maps a neutral ``OrderBy`` to a native / metric trace sort, and the client-side span sort.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_contract.monitoring import MonitoringReadNotSupportedError, OrderBy, SpanWindowItem

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
# get_many has no native sort -> every span-window sort is client-side.
_SPAN_SORT_FIELDS = {"start", "end", "duration", "name", "id"}


def _trace_sort(order_by: OrderBy | None) -> tuple[str, Any]:
    """Resolve a trace sort into a ``(kind, payload)`` pair the caller branches on.

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
    """Sort by ``key`` with ``None``-keyed items always last, so a ``None`` never participates in comparison."""
    present = [it for it in items if key(it) is not None]
    missing = [it for it in items if key(it) is None]
    present.sort(key=key, reverse=reverse)
    return present + missing
