"""A pool host's serving function whose pool, taken for each job, writes an info line in the host."""

from __future__ import annotations

import logging

from tai42_kit.compute import process_pool
from tai42_kit.compute.pool import ComputePool
from tai42_kit.compute.pool_host import serve_pool

HOST_LINE = "the host took its pool"


def _own_pool_with_an_info_line() -> ComputePool:
    logging.getLogger("tai42_kit.compute.pool_host").info(HOST_LINE)
    return process_pool._own_pool()


def serve(fd: int) -> None:
    """Serve the end ``fd`` as a pool host does, writing :data:`HOST_LINE` each time a job takes the pool."""
    serve_pool(fd, _own_pool_with_an_info_line, process_pool.stop_compute_pool)
