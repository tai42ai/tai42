"""CPU-bound work off the event loop: a pool of worker processes per platform process.

:func:`compute_pool` is the process's pool; :class:`ComputePool` runs a picklable job in a
worker and awaits it; ``COMPUTE_WORKERS`` sizes it (:class:`ComputeSettings`).
"""

from tai42_kit.compute.errors import (
    ComputeJobFailedError,
    ComputePoolUnavailableError,
    ComputeWorkerLostError,
    WorkerTraceback,
)
from tai42_kit.compute.pool import ComputePool
from tai42_kit.compute.process_pool import compute_pool, stop_compute_pool
from tai42_kit.compute.settings import ComputeSettings, compute_settings

__all__ = [
    "ComputeJobFailedError",
    "ComputePool",
    "ComputePoolUnavailableError",
    "ComputeSettings",
    "ComputeWorkerLostError",
    "WorkerTraceback",
    "compute_pool",
    "compute_settings",
    "stop_compute_pool",
]
