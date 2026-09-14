"""Identity variant adapters (redis, fixture)."""

from __future__ import annotations

import abc
import json

import psycopg
import redis

from tai42_e2e.topology import Infra, StackResources


class IdentityVariant(abc.ABC):
    """One identity provider: its lifecycle module, the auth-provider selection
    env, and the provider's private root-key seed (which lives here rather than
    in the harness because the wire format is the provider's storage, not a
    harness contract)."""

    name: str
    lifecycle_module: str

    @abc.abstractmethod
    def auth_provider_env(self) -> dict[str, str]:
        """The ``ACCESS_CONTROL_AUTH_PROVIDERS`` selection for this provider — a
        JSON-encoded provider-name list (the setting is a pydantic list)."""

    @abc.abstractmethod
    def seed_identity(self, infra: Infra, resources: StackResources, *, user_id: str, hashed: str) -> None:
        """Write the provider's private identity record for a root key whose raw
        token hashes to ``hashed`` (the harness owns the token + the generic PG
        policy row; the provider owns this storage format)."""


class RedisIdentity(IdentityVariant):
    name = "redis"
    lifecycle_module = "tai42_identity_redis"

    def auth_provider_env(self) -> dict[str, str]:
        return {"ACCESS_CONTROL_AUTH_PROVIDERS": json.dumps(["redis"])}

    def seed_identity(self, infra: Infra, resources: StackResources, *, user_id: str, hashed: str) -> None:
        # The redis identity provider's private storage: a hash at
        # ``ac:key:<sha256(raw)>`` plus the ``ac:management:key:<user>`` reverse
        # lookup on the stack's logical DB.
        client = redis.Redis.from_url(resources.redis_url, decode_responses=True)
        try:
            client.hset(f"ac:key:{hashed}", mapping={"user_id": user_id, "description": "bootstrap"})
            client.set(f"ac:management:key:{user_id}", hashed)
        finally:
            client.close()


# The fixture identity provider's ``fixture_identity_keys`` table, in the stack's
# skeleton store database (its per-run PG clone). The seed writes the provider's wire
# format directly (as the redis seed writes ``ac:key:*`` directly), so this DDL
# and its columns mirror what ``tai42_e2e_fixtures.identity_provider`` reads.
_FIXTURE_IDENTITY_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS fixture_identity_keys ("
    "key_hash TEXT PRIMARY KEY, "
    "user_id TEXT UNIQUE NOT NULL, "
    "description TEXT NOT NULL DEFAULT '', "
    "owner_user_id TEXT"
    ")"
)


class FixtureIdentity(IdentityVariant):
    """The fixture Postgres-backed identity provider — identity provider #2. Its
    records live in a Postgres table, so a stack on it holds NO ``ac:key:*``
    identity records in Redis (the axis-switch proof)."""

    name = "fixture"
    lifecycle_module = "tai42_e2e_fixtures.identity_provider"

    def auth_provider_env(self) -> dict[str, str]:
        return {"ACCESS_CONTROL_AUTH_PROVIDERS": json.dumps(["fixture"])}

    def seed_identity(self, infra: Infra, resources: StackResources, *, user_id: str, hashed: str) -> None:
        # The fixture identity provider's private storage: a row in
        # ``fixture_identity_keys`` in the stack's Postgres database (NOT a Redis
        # ``ac:key:*`` hash). The seed runs before boot, so it ensures the table
        # exists (idempotent) before writing — the provider's own healthcheck
        # ensures it too.
        with psycopg.connect(
            host=resources.pg_host,
            port=resources.pg_port,
            user=resources.pg_user,
            password=resources.pg_password,
            dbname=resources.pg_db,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(_FIXTURE_IDENTITY_CREATE_TABLE)
                cur.execute(
                    "INSERT INTO fixture_identity_keys (key_hash, user_id, description) VALUES (%s, %s, %s) "
                    "ON CONFLICT (user_id) DO UPDATE SET key_hash = EXCLUDED.key_hash, "
                    "description = EXCLUDED.description",
                    (hashed, user_id, "bootstrap"),
                )
            conn.commit()


IDENTITIES: dict[str, IdentityVariant] = {"redis": RedisIdentity(), "fixture": FixtureIdentity()}
