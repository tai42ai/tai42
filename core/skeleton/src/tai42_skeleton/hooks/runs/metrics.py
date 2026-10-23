"""The hook runs' Prometheus metrics, built once on the default registry ``/metrics`` serves.

``prometheus_client`` is imported on first use, never at module import: a worker selects the
multiprocess value backend before its first import of the library.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prometheus_client import Counter, Gauge


@dataclass(frozen=True, slots=True)
class HookRunMetrics:
    """Every hook-run counter and gauge."""

    accepted: Counter
    succeeded: Counter
    attempt_failures: Counter
    failed: Counter
    retried: Counter
    discarded: Counter
    interrupted: Counter
    claim_settles: Counter
    notify_failures: Counter
    sweep_errors: Counter
    runs: Gauge


_lock = threading.Lock()
_metrics: HookRunMetrics | None = None


def hook_run_metrics() -> HookRunMetrics:
    """The process's hook-run metrics, registered on first use."""
    global _metrics
    with _lock:
        if _metrics is None:
            from prometheus_client import Counter, Gauge

            _metrics = HookRunMetrics(
                accepted=Counter("tai42_hook_runs_accepted", "Hook runs kept by an accepted event."),
                succeeded=Counter(
                    "tai42_hook_runs_succeeded", "Hook runs that finished (or whose condition was false)."
                ),
                attempt_failures=Counter(
                    "tai42_hook_runs_attempt_failures", "Failed hook-run attempts, per failure kind.", ["error_kind"]
                ),
                failed=Counter("tai42_hook_runs_failed", "Hook runs held as failed."),
                retried=Counter("tai42_hook_runs_retried", "Failed hook runs retried by an operator."),
                discarded=Counter("tai42_hook_runs_discarded", "Failed hook runs discarded by an operator."),
                interrupted=Counter(
                    "tai42_hook_runs_interrupted",
                    "Hook-run attempts freed with no verdict, per cause (lease, shutdown, drain).",
                    ["cause"],
                ),
                claim_settles=Counter(
                    "tai42_hook_runs_claim_settles",
                    "Unconfirmed resume or take claims settled, per result (open, untold, error).",
                    ["result"],
                ),
                notify_failures=Counter(
                    "tai42_hook_runs_notify_failures", "Failed-run notifications that could not be written."
                ),
                sweep_errors=Counter("tai42_hook_runs_sweep_errors", "Hook-run sweep ticks that raised."),
                runs=Gauge(
                    "tai42_hook_runs",
                    "Hook runs in the store, per status (outstanding, failed).",
                    ["status"],
                    multiprocess_mode="livemax",
                ),
            )
        return _metrics
