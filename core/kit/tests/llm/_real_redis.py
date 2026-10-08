"""The opt-in real Redis (JSON + search modules) the Redis checkpoint tests run against.

The Redis checkpoint saver needs RedisJSON and RediSearch, which no fake provides, so these tests
are opt-in: set ``TAI42_KIT_REAL_REDIS_URL`` to a Redis 8 (or Redis Stack) URL. Without it they
skip visibly with the reason.
"""

from __future__ import annotations

import os

import pytest

REAL_REDIS_ENV = "TAI42_KIT_REAL_REDIS_URL"


def real_redis_url() -> str:
    """The configured real Redis URL; skips the calling test visibly when none is configured."""
    url = os.environ.get(REAL_REDIS_ENV)
    if not url:
        pytest.skip(
            f"real-Redis checkpoint test is opt-in: set {REAL_REDIS_ENV} to a Redis with the JSON and "
            "search modules (Redis 8 or Redis Stack); the checkpoint saver cannot run on a fake"
        )
    return url
