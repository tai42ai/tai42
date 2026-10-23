"""The backoff between connection-establishment retries, shared by the Redis and Postgres seams.

The delay after the ``failures``-th failed attempt doubles from a base and is capped:
16, 32, 64, 128, 256 ms, then 512 ms for every further attempt. The Redis seam builds its
redis-py ``ExponentialBackoff`` from these constants and the Postgres seam sleeps
:func:`connect_retry_delay`, so both seams wait the same. Driver-free: neither seam's
driver is imported here.
"""

from typing import Final

CONNECT_RETRY_BACKOFF_BASE_SECONDS: Final = 0.008
CONNECT_RETRY_BACKOFF_CAP_SECONDS: Final = 0.512


def connect_retry_delay(failures: int) -> float:
    """Seconds to wait after the ``failures``-th failed attempt: ``min(cap, base * 2**failures)``."""
    return min(CONNECT_RETRY_BACKOFF_CAP_SECONDS, CONNECT_RETRY_BACKOFF_BASE_SECONDS * 2**failures)
