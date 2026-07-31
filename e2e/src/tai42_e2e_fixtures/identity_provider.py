"""A fixture Postgres-backed api-key identity provider — the e2e "identity
provider #2".

It exists to prove the identity-axis switch actually switches: it stores its whole
api-key identity record in a Postgres table (``fixture_identity_keys``) instead of
the redis provider's ``ac:key:*`` hashes, so a stack on this provider holds NO
identity records in Redis. Being Postgres-backed it is multi-process-safe by
construction, so the cross-worker auth semantics (provision on A authorizes on B,
revocation propagates) hold exactly as the redis provider's do.

A manifest activates it by naming this module in ``lifecycle_modules`` and setting
``ACCESS_CONTROL_AUTH_PROVIDERS=["fixture"]``; the module-level
:func:`register_identity_provider` call fires at import (no ``tai42_app`` handle
involved, mirroring the redis provider). It reuses the ``ACCESS_CONTROL_STORE_*``
Postgres connection — the same database the policy store lives in — and owns its
own table there. ``readiness_targets`` declares that Postgres store so core's
``/ready`` health-checks the identity provider generically.
"""

from __future__ import annotations

import logging
import secrets

import psycopg
from pydantic_settings import SettingsConfigDict
from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_contract.access_control.identity import (
    ApiKeyIdentityProvider,
    AuthIdentity,
    IdentityProviderSettings,
    ReadinessTarget,
)
from tai42_contract.access_control.registry import register_identity_provider
from tai42_kit.clients import PostgresConnectionSettings, client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.settings import settings_cache
from tai42_kit.utils.data.string_util import hash_api_key

logger = logging.getLogger(__name__)

# The provider's OWN table in the ACCESS_CONTROL_STORE Postgres database, created by the
# harness before boot: the key hash (primary key), the user it identifies, and its
# description. The ``UNIQUE (user_id)`` constraint is the atomic guard ``provision`` relies
# on so two concurrent provisions of one user cannot both write.
_TABLE = "fixture_identity_keys"


class FixtureIdentityPgSettings(PostgresConnectionSettings):
    """The Postgres connection for the fixture identity store, read from the
    ``ACCESS_CONTROL_STORE_*`` env — the same database the policy store uses, so a
    stack that wires the policy store already points this provider at its PG."""

    model_config = SettingsConfigDict(env_prefix="ACCESS_CONTROL_STORE_")

    pg_db: str = "tai"


@settings_cache
def fixture_identity_pg_settings() -> FixtureIdentityPgSettings:
    return FixtureIdentityPgSettings()


class DuplicateIdentityError(ValueError):
    """``provision`` was called for a ``user_id`` that already has an identity
    record — a ``ValueError`` so the provisioning surface's duplicate handling
    catches it uniformly."""


def _generate_api_key() -> str:
    return f"sk-{secrets.token_urlsafe(32)}"


class FixturePgApiKeyProvider(ApiKeyIdentityProvider):
    """Validate and provision api keys against a Postgres table."""

    def __init__(self, settings: IdentityProviderSettings) -> None:
        # The registry hands every provider the access-control settings shape, but this
        # Postgres-backed provider reads its own PG connection from the ACCESS_CONTROL_STORE
        # env instead, so that shape carries nothing it needs.
        del settings

    def _pg(self) -> FixtureIdentityPgSettings:
        return fixture_identity_pg_settings()

    async def validate_token(self, token: str) -> AuthIdentity | None:
        hashed = hash_api_key(token)
        async with (
            client_ctx(PostgresClient, self._pg()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(
                f"SELECT user_id, description, owner_user_id FROM {_TABLE} WHERE key_hash = %s", (hashed,)
            )
            row = await cur.fetchone()
        if row is None:
            return None
        user_id, description, owner_user_id = row
        claims: dict[str, str] = {"user_id": user_id, "description": description}
        if owner_user_id is not None:
            claims[OWNER_USER_ID_CLAIM] = owner_user_id
        return AuthIdentity(user_id=user_id, claims=claims)

    async def provision(self, user_id: str, description: str, *, owner_user_id: str | None = None) -> str:
        raw_key = _generate_api_key()
        hashed = hash_api_key(raw_key)
        async with (
            client_ctx(PostgresClient, self._pg()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            try:
                await cur.execute(
                    f"INSERT INTO {_TABLE} (key_hash, user_id, description, owner_user_id) VALUES (%s, %s, %s, %s)",
                    (hashed, user_id, description, owner_user_id),
                )
            except psycopg.errors.UniqueViolation as e:
                # The ``UNIQUE (user_id)`` constraint is the atomic duplicate
                # authority: a racing second provision of the same user fails here
                # rather than stranding a second, unrevocable identity record.
                await conn.rollback()
                raise DuplicateIdentityError(
                    f"user id {user_id!r} already has an identity record; revoke it before minting a replacement"
                ) from e
        return raw_key

    async def revoke(self, user_id: str) -> bool:
        async with (
            client_ctx(PostgresClient, self._pg()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(f"DELETE FROM {_TABLE} WHERE user_id = %s", (user_id,))
            return cur.rowcount > 0

    async def update_description(self, user_id: str, description: str) -> bool:
        async with (
            client_ctx(PostgresClient, self._pg()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(f"UPDATE {_TABLE} SET description = %s WHERE user_id = %s", (description, user_id))
            return cur.rowcount > 0

    async def list_identities(self) -> list[tuple[str, str]]:
        async with (
            client_ctx(PostgresClient, self._pg()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(f"SELECT user_id, description FROM {_TABLE}")
            rows = await cur.fetchall()
        return [(user_id, description) for user_id, description in rows]

    async def healthcheck(self) -> None:
        # Probe the provider's OWN table so a missing or broken identity store fails boot
        # loudly instead of at the first request. The harness seed creates it, so a provider
        # that cannot see its table is a real misconfiguration.
        async with (
            client_ctx(PostgresClient, self._pg()) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            await cur.execute(f"SELECT 1 FROM {_TABLE} LIMIT 1")

    def readiness_targets(self) -> tuple[ReadinessTarget]:
        # Declare the provider's OWN Postgres store as core's readiness target. Core pings
        # it generically (deduped against every other subsystem sharing the connection), so
        # the identity store is health-checked without core knowing it is Postgres.
        return (ReadinessTarget("access_control", PostgresClient, self._pg()),)


# Module-level registration under the ``fixture`` provider name — fires at import with no
# ``tai42_app`` handle, so it fills the registry in any process that imports this module.
register_identity_provider("fixture", FixturePgApiKeyProvider)
