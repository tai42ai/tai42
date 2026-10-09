"""The outbox's Prometheus metrics, built once on the default registry ``/metrics`` serves.

``prometheus_client`` is imported on first use, never at module import: a worker selects the
multiprocess value backend before its first import of the library.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prometheus_client import Counter, Gauge, Histogram


@dataclass(frozen=True, slots=True)
class OutboxMetrics:
    """Every outbox counter, histogram and gauge."""

    enqueued: Counter
    applied: Counter
    attempt_failures: Counter
    failed: Counter
    divergences: Counter
    discarded: Counter
    retried: Counter
    lock_waits: Counter
    notify_failures: Counter
    sweep_errors: Counter
    drain_waits: Counter
    drain_wait_seconds: Histogram
    apply_latency_seconds: Histogram
    rows: Gauge
    oldest_age_seconds: Gauge


_lock = threading.Lock()
_metrics: OutboxMetrics | None = None


def outbox_metrics() -> OutboxMetrics:
    """The process's outbox metrics, registered on first use."""
    global _metrics
    with _lock:
        if _metrics is None:
            from prometheus_client import Counter, Gauge, Histogram

            _metrics = OutboxMetrics(
                enqueued=Counter("tai42_state_outbox_enqueued", "Pending state saves enqueued."),
                applied=Counter("tai42_state_outbox_applied", "Pending state saves applied, per phase.", ["phase"]),
                attempt_failures=Counter(
                    "tai42_state_outbox_attempt_failures",
                    "Failed attempts to apply a pending state save, per phase and failure kind.",
                    ["phase", "kind"],
                ),
                failed=Counter(
                    "tai42_state_outbox_failed", "Pending state saves that failed and hold their subjects.", ["phase"]
                ),
                divergences=Counter(
                    "tai42_state_outbox_divergences", "Pending state saves whose apply diverged from the projection."
                ),
                discarded=Counter("tai42_state_outbox_discarded", "Failed pending state saves discarded."),
                retried=Counter("tai42_state_outbox_retried", "Failed pending state saves retried."),
                lock_waits=Counter(
                    "tai42_state_outbox_lock_waits", "Applies that found a subject lock held past the lock timeout."
                ),
                notify_failures=Counter(
                    "tai42_state_outbox_notify_failures", "Failed-save notifications that could not be written."
                ),
                sweep_errors=Counter("tai42_state_outbox_sweep_errors", "Outbox sweep passes that raised."),
                drain_waits=Counter(
                    "tai42_state_outbox_drain_waits",
                    "Reads and run entries that waited for a subject's pending save.",
                    ["door"],
                ),
                drain_wait_seconds=Histogram(
                    "tai42_state_outbox_drain_wait_seconds",
                    "Time a read or run entry waited for a subject's pending save.",
                    ["door"],
                ),
                apply_latency_seconds=Histogram(
                    "tai42_state_outbox_apply_latency_seconds",
                    "Time from a pending save's enqueue to its records applied.",
                ),
                rows=Gauge(
                    "tai42_state_outbox_rows",
                    "Outstanding pending state saves, per status.",
                    ["status"],
                    multiprocess_mode="livemax",
                ),
                oldest_age_seconds=Gauge(
                    "tai42_state_outbox_oldest_age_seconds",
                    "Age of the oldest outstanding pending state save.",
                    multiprocess_mode="livemax",
                ),
            )
        return _metrics
