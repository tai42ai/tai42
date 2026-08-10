"""Redis-backed api-key identity provider.

Owns the whole api-key identity record — the ``ac:key:{sha256(raw)}`` hash
(key hash -> ``{user_id, description}``) plus the ``user_id -> hash`` reverse
lookup. Token validation is the read side; provision/revoke/description edits are
the write side, both against the provider's own plain-Redis storage. Registers
itself as the ``"redis"`` identity provider at import.
"""

from __future__ import annotations

import logging
import secrets
from typing import cast

from redis.exceptions import WatchError
from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_contract.access_control.identity import (
    ApiKeyIdentityProvider,
    AuthIdentity,
    IdentityProviderSettings,
    ReadinessTarget,
)
from tai42_contract.access_control.registry import register_identity_provider
from tai42_kit.clients import RedisConnectionSettings, client_ctx
from tai42_kit.clients.impl.redis import RedisClient, hgetall, hset_mapping, scan_iter
from tai42_kit.utils.data.string_util import hash_api_key

logger = logging.getLogger(__name__)

# The ``user_id -> hashed-key`` reverse lookup, so a user id resolves to its stored
# hash for revoke/edit without a scan. Its value is the plain hash string.
_REVERSE_KEY_PREFIX = "ac:management:key:"

# Namespaced probe key the healthcheck reads; ``HGETALL`` on a missing key is fine.
_PROBE_KEY = "ac:__identity_probe__"


class DuplicateIdentityError(ValueError):
    """``provision`` was called for a ``user_id`` that already has an identity
    record. A ``ValueError`` so the duplicate handling catches it uniformly."""


def _generate_api_key() -> str:
    return f"sk-{secrets.token_urlsafe(32)}"


class RedisApiKeyProvider(ApiKeyIdentityProvider):
    """Validate and provision api keys against plain Redis hashes."""

    def __init__(self, settings: IdentityProviderSettings) -> None:
        self.settings = settings

    def _identity_key(self, hashed: str) -> str:
        return f"{self.settings.key_prefix}{hashed}"

    def _reverse_key(self, user_id: str) -> str:
        return f"{_REVERSE_KEY_PREFIX}{user_id}"

    def _redis_settings(self) -> RedisConnectionSettings:
        # Bridge the contract's ``Any`` redis to kit's nominal settings type.
        return cast("RedisConnectionSettings", self.settings.redis)

    async def validate_token(self, token: str) -> AuthIdentity | None:
        hashed = hash_api_key(token)
        key = self._identity_key(hashed)

        # Fail closed: a backend error RAISES (never returns None, which reads as an
        # invalid credential). A successful read with no stored identity returns None.
        try:
            async with client_ctx(RedisClient, self._redis_settings()) as r:
                data = await hgetall(r, key)
        except Exception as e:
            logger.error(f"Error validating redis token: {e}")
            raise

        if not data or "user_id" not in data:
            return None

        return AuthIdentity(
            user_id=data["user_id"],
            claims=data,  # Pass all metadata as claims
        )

    async def provision(self, user_id: str, description: str, *, owner_user_id: str | None = None) -> str:
        raw_key = _generate_api_key()
        hashed = hash_api_key(raw_key)
        reverse_key = self._reverse_key(user_id)
        identity_key = self._identity_key(hashed)

        # An owned key carries the owner claim in the same hash (omitted entirely
        # when ownerless, never a ``None`` value), so no spurious owner claim reaches
        # the auth decision.
        identity_record = {"user_id": user_id, "description": description}
        if owner_user_id is not None:
            identity_record[OWNER_USER_ID_CLAIM] = owner_user_id

        # WATCH the reverse lookup, refuse if the user already has a record, then
        # commit the identity hash and reverse lookup in one MULTI — the atomic
        # per-user duplicate guard. Two concurrent provisions cannot both write: the
        # loser's EXEC aborts (``WatchError``) and re-reads to find the record present.
        async with client_ctx(RedisClient, self._redis_settings()) as r, r.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(reverse_key)
                    if await pipe.get(reverse_key):
                        await pipe.unwatch()
                        raise DuplicateIdentityError(
                            f"user id {user_id!r} already has an identity record; revoke it "
                            "before minting a replacement"
                        )
                    pipe.multi()
                    hset_mapping(pipe, identity_key, identity_record)
                    pipe.set(reverse_key, hashed)
                    await pipe.execute()
                    break
                except WatchError:
                    # A concurrent writer changed the reverse key; re-read and re-decide.
                    continue

        return raw_key

    async def revoke(self, user_id: str) -> bool:
        reverse_key = self._reverse_key(user_id)
        async with client_ctx(RedisClient, self._redis_settings()) as r:
            # The reverse lookup holds the plain hash string (pin the loose read to str).
            hashed = cast("str | None", await r.get(reverse_key))
            if not hashed:
                return False
            await r.delete(self._identity_key(hashed), reverse_key)
        return True

    async def update_description(self, user_id: str, description: str) -> bool:
        async with client_ctx(RedisClient, self._redis_settings()) as r:
            hashed = cast("str | None", await r.get(self._reverse_key(user_id)))
            if not hashed:
                return False
            identity_key = self._identity_key(hashed)
            identity = await hgetall(r, identity_key)
            if not identity:
                return False
            # Overwrite only ``description``; the pre-write existence check avoids
            # creating a partial record on a missing key.
            await hset_mapping(r, identity_key, {"description": description})
        return True

    async def list_identities(self) -> list[tuple[str, str]]:
        identities: list[tuple[str, str]] = []
        async with client_ctx(RedisClient, self._redis_settings()) as r:
            async for key in scan_iter(r, f"{self.settings.key_prefix}*"):
                item = await hgetall(r, key)
                if not item:
                    continue
                user_id = item.get("user_id")
                if not user_id:
                    continue
                identities.append((user_id, item.get("description", "")))
        return identities

    async def healthcheck(self) -> None:
        # Probe the record store with token validation's exact read shape (one
        # ``HGETALL``); any Redis error propagates so a broken store fails startup.
        async with client_ctx(RedisClient, self._redis_settings()) as r:
            await hgetall(r, _PROBE_KEY)

    def readiness_targets(self) -> tuple[ReadinessTarget]:
        # The provider's own Redis record store, pinged generically by core under the
        # "access_control" label.
        return (ReadinessTarget("access_control", RedisClient, self._redis_settings()),)


# Registers itself as "redis" at import (no ``tai42_app`` handle — the plugin must
# register in processes that never ``start()``). The factory is the class itself.
register_identity_provider("redis", RedisApiKeyProvider)
