"""CPU-bound work off the event loop: a pool of worker processes, shared with the children a process forks.

:func:`compute_pool` is what runs the process's jobs; :class:`ComputePool` runs a picklable job
in a worker and awaits it; ``COMPUTE_WORKERS`` sizes it (:class:`ComputeSettings`).
:func:`share_compute_pool` lets the children a process forks run their jobs in its pool. A process
started through a process prelude runs its jobs in a pool host it starts, through a
:class:`HostedPool`. A process reaches a pool it does not run through a :class:`ComputePoolClient`.
"""

from tai42_kit.compute.errors import (
    ComputeJobFailedError,
    ComputePoolUnavailableError,
    ComputeWorkerLostError,
    WorkerTraceback,
)
from tai42_kit.compute.pool import ComputePool
from tai42_kit.compute.pool_host import HostedPool
from tai42_kit.compute.pool_wire import ComputePoolClient
from tai42_kit.compute.process_pool import compute_pool, share_compute_pool, stop_compute_pool
from tai42_kit.compute.settings import ComputeSettings, compute_settings

__all__ = [
    "ComputeJobFailedError",
    "ComputePool",
    "ComputePoolClient",
    "ComputePoolUnavailableError",
    "ComputeSettings",
    "ComputeWorkerLostError",
    "HostedPool",
    "WorkerTraceback",
    "compute_pool",
    "compute_settings",
    "share_compute_pool",
    "stop_compute_pool",
]
