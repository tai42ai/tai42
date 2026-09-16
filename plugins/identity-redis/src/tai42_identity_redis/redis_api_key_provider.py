"""Redis-backed api-key identity provider.

Owns the whole api-key identity record — the ``ac:key:{sha256(raw)}`` hash
(key hash -> ``{user_id, owner_user_id, description}``) plus the ``user_id -> hash``
reverse lookup. Token validation is the read side; provision/revoke/description
edits are the write side, both against the provider's own plain-Redis storage.
Every api key belongs to a principal, so every identity record carries the owner
claim; a stored record missing it is an invariant breach the read paths refuse
loudly. Registers itself as the ``"redis"`` identity provider at import.
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
    """``provision`` was called for a ``user_id`` that already has an identity record.

    A ``ValueError`` so the duplicate handling catches it uniformly.
    """


class OwnerlessIdentityError(ValueError):
    """A stored api-key identity record carries no owner claim — an invariant breach.

    Every api key belongs to a principal, so every identity record must carry
    :data:`OWNER_USER_ID_CLAIM`. A stored record missing it is corrupt: the read
    paths refuse it loudly rather than resolving it or silently defaulting a
    missing owner. A ``ValueError`` so the read paths' fail-closed handling catches
    it uniformly; on the token-validation path it propagates into the application's
    fail-closed deny (never an allow), never a request-path 500.
    """


def _generate_api_key() -> str:
    return f"sk-{secrets.token_urlsafe(32)}"


def _require_owner_claim(record: dict[str, str], user_id: str) -> None:
    """Refuse a stored identity ``record`` that carries no owner claim.

    Logged at error level at the source so the invariant breach is visible before
    it propagates, then raises :class:`OwnerlessIdentityError`. Called by every read
    path that materializes a stored record, so a claim-less record can never surface
    as a resolved identity or a silent default.
    """
    if not record.get(OWNER_USER_ID_CLAIM):
        logger.error(
            "identity record for user %s carries no owner claim — ownerless key record; refusing",
            user_id,
        )
        raise OwnerlessIdentityError(
            f"identity record for user id {user_id!r} carries no owner claim; every api key belongs to a principal"
        )


class RedisApiKeyProvider(ApiKeyIdentityProvider):
    """Validate and provision api keys against plain Redis hashes."""

    def __init__(self, settings: IdentityProviderSettings) -> None:
        """Store the provider ``settings``."""
        self.settings = settings

    def _identity_key(self, hashed: str) -> str:
        return f"{self.settings.key_prefix}{hashed}"

    def _reverse_key(self, user_id: str) -> str:
        return f"{_REVERSE_KEY_PREFIX}{user_id}"

    def _redis_settings(self) -> RedisConnectionSettings:
        # Bridge the contract's ``Any`` redis to kit's nominal settings type.
        return cast("RedisConnectionSettings", self.settings.redis)

    async def validate_token(self, token: str) -> AuthIdentity | None:
        """Resolve ``token`` to an :class:`AuthIdentity`, or ``None`` when no identity is stored.

        A backend error raises rather than reading as an invalid credential.
        """
        hashed = hash_api_key(token)
        key = self._identity_key(hashed)

        # Fail closed: a backend error RAISES (never returns None, which reads as an
        # invalid credential). A successful read with no stored identity returns None.
        try:
            async with client_ctx(RedisClient, self._redis_settings()) as r:
                data = await hgetall(r, key)
        except Exception:
            logger.exception("Error validating redis token")
            raise

        if not data or "user_id" not in data:
            return None

        # Every api key belongs to a principal: a resolved record MUST carry the
        # owner claim. A record with a user_id but no owner claim is corrupt — refuse
        # it loudly (the application catches the raise into a fail-closed deny), never
        # resolve it as a valid identity.
        _require_owner_claim(data, data["user_id"])

        return AuthIdentity(
            user_id=data["user_id"],
            claims=data,  # Pass all metadata as claims
        )

    async def provision(self, user_id: str, description: str, *, owner_user_id: str) -> str:
        """Mint a new api key owned by ``owner_user_id`` for ``user_id`` and return the raw key.

        ``owner_user_id`` is REQUIRED — every api key belongs to a principal — and is
        written into the identity record under :data:`OWNER_USER_ID_CLAIM`, so
        ``validate_token`` surfaces it in ``AuthIdentity.claims`` for the application's
        per-request owner attenuation. A ``None`` or empty owner is a ``ValueError``
        raised BEFORE any write.

        A ``user_id`` that already has an identity raises :class:`DuplicateIdentityError`.
        """
        if not owner_user_id:
            raise ValueError(
                f"owner_user_id is required to provision an api key for user id {user_id!r}; "
                "every api key belongs to a principal"
            )

        raw_key = _generate_api_key()
        hashed = hash_api_key(raw_key)
        reverse_key = self._reverse_key(user_id)
        identity_key = self._identity_key(hashed)

        # Every api key belongs to a principal, so the owner claim is always written
        # into the identity record; it surfaces in ``AuthIdentity.claims`` on every
        # ``validate_token`` for the application's per-request owner attenuation.
        identity_record = {
            "user_id": user_id,
            "description": description,
            OWNER_USER_ID_CLAIM: owner_user_id,
        }

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
        """Delete ``user_id``'s identity record and reverse lookup; return whether one existed."""
        reverse_key = self._reverse_key(user_id)
        async with client_ctx(RedisClient, self._redis_settings()) as r:
            # The reverse lookup holds the plain hash string (pin the loose read to str).
            hashed = cast("str | None", await r.get(reverse_key))
            if not hashed:
                return False
            await r.delete(self._identity_key(hashed), reverse_key)
        return True

    async def update_description(self, user_id: str, description: str) -> bool:
        """Update ``user_id``'s stored description; return whether the identity existed."""
        async with client_ctx(RedisClient, self._redis_settings()) as r:
            hashed = cast("str | None", await r.get(self._reverse_key(user_id)))
            if not hashed:
                return False
            identity_key = self._identity_key(hashed)
            identity = await hgetall(r, identity_key)
            if not identity:
                return False
            # A present record MUST carry the owner claim (every api key belongs to a
            # principal). Refuse a claim-less record loudly rather than editing corrupt
            # storage in place.
            _require_owner_claim(identity, user_id)
            # Overwrite only ``description``; the pre-write existence check avoids
            # creating a partial record on a missing key.
            await hset_mapping(r, identity_key, {"description": description})
        return True

    async def list_identities(self) -> list[tuple[str, str]]:
        """Every stored identity as ``(user_id, description)`` pairs."""
        identities: list[tuple[str, str]] = []
        async with client_ctx(RedisClient, self._redis_settings()) as r:
            async for key in scan_iter(r, f"{self.settings.key_prefix}*"):
                item = await hgetall(r, key)
                if not item:
                    continue
                user_id = item.get("user_id")
                if not user_id:
                    continue
                # Every enumerated record MUST carry the owner claim. A claim-less
                # record is an invariant breach: refuse the enumeration loudly rather
                # than silently omitting it (which would read as an orphaned key).
                _require_owner_claim(item, user_id)
                identities.append((user_id, item.get("description", "")))
        return identities

    async def healthcheck(self) -> None:
        """Probe the Redis record store with token validation's read shape; any error propagates."""
        # Probe the record store with token validation's exact read shape (one
        # ``HGETALL``); any Redis error propagates so a broken store fails startup.
        async with client_ctx(RedisClient, self._redis_settings()) as r:
            await hgetall(r, _PROBE_KEY)

    def readiness_targets(self) -> tuple[ReadinessTarget]:
        """The provider's Redis record store as a readiness target under the ``access_control`` label."""
        # The provider's own Redis record store, pinged generically by core under the
        # "access_control" label.
        return (ReadinessTarget("access_control", RedisClient, self._redis_settings()),)


# Registers itself as "redis" at import (no ``tai42_app`` handle — the plugin must
# register in processes that never ``start()``). The factory is the class itself.
register_identity_provider("redis", RedisApiKeyProvider)
