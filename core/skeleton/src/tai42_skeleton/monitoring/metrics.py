"""The ``/metrics`` counters of what the monitoring writer could not deliver.

Built lazily on ``prometheus_client``'s default registry: in a multiprocess run family each
process writes its own per-pid file in ``PROMETHEUS_MULTIPROC_DIR`` (which outlives the
process) and every ``/metrics`` scrape merges them; in an in-process app they live in the
default registry. ``prometheus_client`` is imported only when a counter is first built: the
library freezes its value backend at first import from ``PROMETHEUS_MULTIPROC_DIR``, which a
writer process sets during boot.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prometheus_client import Counter

# The one reason a span is dropped before export.
QUEUE_FULL_REASON = "queue_full"


@lru_cache(maxsize=1)
def spans_dropped() -> Counter:
    """Spans evicted by a full export queue (``tai42_monitoring_spans_dropped_total{reason}``)."""
    from prometheus_client import Counter

    return Counter("tai42_monitoring_spans_dropped", "Monitoring spans dropped before export", ["reason"])


@lru_cache(maxsize=1)
def spans_failed() -> Counter:
    """Spans in an export batch the exporter could not deliver."""
    from prometheus_client import Counter

    return Counter("tai42_monitoring_spans_failed", "Monitoring spans the exporter could not deliver")


@lru_cache(maxsize=1)
def export_failures() -> Counter:
    """Export calls that failed (a FAILURE result or an exception)."""
    from prometheus_client import Counter

    return Counter("tai42_monitoring_export_failures", "Monitoring export calls that failed")


@lru_cache(maxsize=1)
def attributes_dropped() -> Counter:
    """Span attributes evicted over the span limit."""
    from prometheus_client import Counter

    return Counter("tai42_monitoring_attributes_dropped", "Monitoring span attributes dropped over the limit")


@lru_cache(maxsize=1)
def records_failed() -> Counter:
    """Records (or one field of a record) the writer could not build."""
    from prometheus_client import Counter

    return Counter("tai42_monitoring_records_failed", "Monitoring records the writer could not build")


def increment(field: str, amount: int) -> None:
    """Add ``amount`` to the counter of the ``MonitoringExportHealth`` field ``field``."""
    if field == "spans_dropped":
        spans_dropped().labels(reason=QUEUE_FULL_REASON).inc(amount)
    else:
        _COUNTERS[field]().inc(amount)


_COUNTERS = {
    "spans_failed": spans_failed,
    "export_failures": export_failures,
    "attributes_dropped": attributes_dropped,
    "records_failed": records_failed,
}

# The exposed sample name of each field's counter.
SAMPLE_NAMES: dict[str, str] = {
    "spans_dropped": "tai42_monitoring_spans_dropped_total",
    "spans_failed": "tai42_monitoring_spans_failed_total",
    "export_failures": "tai42_monitoring_export_failures_total",
    "attributes_dropped": "tai42_monitoring_attributes_dropped_total",
    "records_failed": "tai42_monitoring_records_failed_total",
}
