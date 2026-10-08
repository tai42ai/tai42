"""The checkpoint sweep's Prometheus counter, served by the multiprocess-aware ``GET /metrics``."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prometheus_client import Counter


@lru_cache(maxsize=1)
def sweep_spared_counter() -> Counter:
    """Threads past a retention horizon that a live-thread filter claimed, by horizon and owner.

    Built lazily: ``prometheus_client`` freezes its value backend at first import, which must
    follow the multiprocess directory setup.
    """
    from prometheus_client import Counter

    return Counter(
        "tai42_checkpoint_sweep_spared_total",
        "Checkpoint threads past a retention horizon kept because a live-thread filter reports them live",
        ["horizon", "owner"],
    )
