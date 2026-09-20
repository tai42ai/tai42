"""tai42-identity-redis: the Redis-backed api-key identity provider plugin.

Importing this package registers the ``"redis"`` identity provider in the
contract's module-level registry (see :mod:`tai42_identity_redis.redis_api_key_provider`).
"""

from __future__ import annotations

from tai42_identity_redis.redis_api_key_provider import RedisApiKeyProvider

__all__ = [
    "RedisApiKeyProvider",
]
