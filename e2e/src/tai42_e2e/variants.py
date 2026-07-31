"""The variant adapter layer: one small object per backend / identity / storage
axis, holding everything plugin-specific that would otherwise leak into
``manifests.py``, ``harness.py``, ``redisx.py``, and the reload spec.

One variant set is resolved per pytest process from the ``TAI_E2E_`` selection
env vars (``resolve_variants``); an unknown name raises loudly at session start
naming the valid values. Adding a fourth backend is one adapter class plus one
registry entry — no edits to the boot engine or the manifest builders.

The live-fleet census is backend-INDEPENDENT: it reads the app-owned worker bus
(:func:`bus_census`), a scan of the per-origin presence keys that every subscribed
process — HTTP ``serve`` worker and backend runtime alike — advertises. It is a
module function, not a per-backend method, because the bus is app infrastructure
the plugin does not own. The harness never imports a system-under-test package to
read it: it scans the presence keys off the bus Redis through the ``redis`` client.
"""

from __future__ import annotations

import abc
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import redis

from tai42_e2e.rabbitx import RabbitAdmin, broker_url_for
from tai42_e2e.stack import Infra, InfraUnavailable, StackResources

if TYPE_CHECKING:
    from tai42_e2e.settings import HarnessSettings


# ---- app worker bus census ----------------------------------------------


@dataclass(frozen=True)
class BusOrigin:
    """One live worker on the app-owned worker bus, parsed from a presence key +
    value: the ``{kind}-{uuid}`` origin id, the worker ``kind`` (``serve`` for an
    HTTP worker, ``backend`` for a runtime worker), and the ``pid`` the presence
    value carries."""

    origin: str
    kind: str
    pid: int


def bus_census(bus_redis_url: str, namespace: str) -> list[BusOrigin]:
    """The live fleet on the worker bus: scan the per-origin presence keys under
    this stack's namespace on the bus Redis and parse each value's ``{kind, pid}``.

    Backend-independent — every subscribed origin (both the HTTP ``serve`` workers
    and the ``backend`` runtime) advertises exactly one presence key, so this is
    the whole fleet the reload/readiness seams draw from, read straight off the bus
    Redis (the harness never imports the system under test). A key that expires
    between the scan and the value read is skipped; a malformed value raises loudly
    (a stack scans only its own namespace, so it sees only its own well-formed keys)."""
    prefix = f"{namespace}:bus:presence:"
    client = redis.Redis.from_url(bus_redis_url, decode_responses=True)
    try:
        origins: list[BusOrigin] = []
        for key in client.scan_iter(match=f"{prefix}*"):
            value = client.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise TypeError(f"presence value for {key!r} is not a decoded string: {type(value)!r}")
            meta = json.loads(value)
            origins.append(BusOrigin(origin=key[len(prefix) :], kind=meta["kind"], pid=int(meta["pid"])))
        return origins
    finally:
        client.close()


def short_presence_ttl_env(seconds: float) -> dict[str, str]:
    """Env that makes a frozen or killed worker leave the bus census within
    ~``seconds``: a subscriber refreshes its presence key at a third of
    ``TAI_BUS_HEARTBEAT_TTL``, so a stopped worker's key expires within one TTL.

    Backend-independent — presence + its TTL live on the app-owned bus, not in any
    plugin, so the same env governs every backend and every worker kind."""
    return {"TAI_BUS_HEARTBEAT_TTL": str(seconds)}


# ---- broker leases ------------------------------------------------------


@dataclass(frozen=True)
class BrokerLease:
    """A per-stack broker reservation. ``release()`` reaps the isolated broker
    resource in ``TaiStack.teardown``'s error-collecting block, symmetric to the
    per-stack Postgres database drop."""

    broker_url: str
    admin: RabbitAdmin
    vhost: str

    def release(self) -> None:
        self.admin.delete_vhost(self.vhost)


# ---- backend axis -------------------------------------------------------


class BackendVariant(abc.ABC):
    """One Backend plugin: the manifest ``backend_module`` string, the feature
    env pointing it at a stack's isolated resources, and its process model. The
    live-fleet census is NOT here — it reads the app-owned bus (:func:`bus_census`),
    the same for every backend."""

    name: str
    module: str
    # The Backend class + defining module the ``/api/backend`` identity door reports
    # for this plugin (the door names the REGISTERED class, which lives in the
    # plugin's implementation module, not necessarily its package root).
    provider_class: str
    provider_module: str

    @abc.abstractmethod
    def feature_env(self, res: StackResources) -> dict[str, str]:
        """The backend plugin's env group, pointed at this stack's resources."""

    def infra_check(self, settings: HarnessSettings) -> None:
        """Extra reachability beyond the shared Redis + Postgres. The default is
        a no-op (Redis-only backends); overridden where a backend needs its own
        broker, raising :class:`InfraUnavailable` with the compose hint."""
        return None

    def allocate_broker(self, infra: Infra, stack_id: str) -> BrokerLease | None:
        """Reserve this stack's isolated broker resource. The default backend
        rides on the shared Redis and leases nothing (``None``)."""
        return None

    @abc.abstractmethod
    def extra_backend_processes(self) -> list[list[str]]:
        """The extra ``tai backend <args>`` invocations a ``run_backend`` stack
        must spawn alongside the worker — each one a separate process the boot
        engine gives its own ProcessHandle, log capture, and teardown leak-reap.

        Empty (``[]``) is the explicit "the worker hosts everything" answer, not a
        default escape hatch: arq's recurring scheduler is a self-rescheduling
        queue job the worker runs, whereas celery's RedBeat and rq's rq-scheduler
        each need their own long-lived process before a ``schedule_task`` schedule
        can ever fire."""

    @abc.abstractmethod
    def task_timeout_env(self, seconds: int) -> dict[str, str]:
        """Env that bounds a backend-execution (``sync_task``) result wait to
        ~``seconds``. The worker-crash spec sets it low so a job orphaned by a
        SIGKILLed worker surfaces a bounded, observable terminal instead of
        blocking on the multi-minute production default."""

    # The tool-run status an in-flight job reaches when the ``tai backend worker`` process
    # group is SIGKILLed under it. The invariant is always a bounded, loud terminal, never
    # an eternal ``running``, but the concrete outcome is a property of each backend's
    # process model, so each variant declares its own and the spec asserts exactly that.
    crashed_run_terminal: str


class ArqVariant(BackendVariant):
    name = "arq"
    module = "tai42_backend_arq"
    provider_class = "ArqBackend"
    provider_module = "tai42_backend_arq.backend"
    # The worker process itself executes the job, so killing its process group
    # orphans the job: no in-worker monitor survives to fail it, the sync_task result
    # wait times out at ``task_timeout``, and the run is recorded failed.
    crashed_run_terminal = "failed"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        return {"ARQ_REDIS_URL": res.redis_url}

    def extra_backend_processes(self) -> list[list[str]]:
        # arq's recurring scheduler is the self-rescheduling ``task_scheduler``
        # queue job the worker runs, so a schedule fires without any extra process.
        return []

    def task_timeout_env(self, seconds: int) -> dict[str, str]:
        return {"ARQ_TASK_TIMEOUT": str(seconds)}


class RqVariant(BackendVariant):
    name = "rq"
    module = "tai42_backend_rq"
    provider_class = "RqBackend"
    provider_module = "tai42_backend_rq.backend"
    # RQ runs each job in a work-horse child in its OWN process group (``os.setpgrp``),
    # outside the worker master's group: killing the master leaves the horse running, it
    # finishes the job and writes the result, and the run is recorded succeeded.
    crashed_run_terminal = "succeeded"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        return {"RQ_REDIS_URL": res.redis_url}

    def extra_backend_processes(self) -> list[list[str]]:
        # ``schedule_task`` recurring jobs only reach the queue when the ``rqscheduler``
        # daemon moves them (the worker's own ``with_scheduler`` covers only one-shot
        # ``enqueue_at`` jobs). ``tai backend beat`` runs that daemon; ``-i 1`` polls once
        # a second so a short-interval schedule fires promptly.
        return [["beat", "-i", "1"]]

    def task_timeout_env(self, seconds: int) -> dict[str, str]:
        return {"RQ_TASK_TIMEOUT": str(seconds)}


class CeleryVariant(BackendVariant):
    name = "celery"
    module = "tai42_backend_celery"
    provider_class = "CeleryBackend"
    provider_module = "tai42_backend_celery.core.backend"
    # The prefork child executing the job lives in the worker's process group, so
    # killing the group takes the job with it: the sync_task result wait times out at
    # ``task_timeout`` and the run is recorded failed.
    crashed_run_terminal = "failed"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        broker_url = self._require_broker(res)
        return {
            "CELERY_BROKER_URL": broker_url,
            "CELERY_RESULT_BACKEND": res.redis_url,
            "CELERY_REDBEAT_REDIS_URL": res.redis_url,
        }

    def infra_check(self, settings: HarnessSettings) -> None:
        admin = RabbitAdmin(settings.rabbitmq_management_url)
        try:
            admin.check_reachable()
        except Exception as exc:
            raise InfraUnavailable(
                f"RabbitMQ not reachable ({exc}). Start it with `docker compose --profile celery up -d`."
            ) from exc

    def allocate_broker(self, infra: Infra, stack_id: str) -> BrokerLease | None:
        settings = infra.settings
        admin = RabbitAdmin(settings.rabbitmq_management_url)
        vhost = f"tai42_e2e_{stack_id}"
        admin.create_vhost(vhost)
        return BrokerLease(broker_url=broker_url_for(settings.rabbitmq_url, vhost), admin=admin, vhost=vhost)

    def extra_backend_processes(self) -> list[list[str]]:
        # RedBeat schedules only fire while a beat process runs; ``tai backend
        # beat`` starts it (RedBeat reads CELERY_REDBEAT_REDIS_URL from the env).
        # ``--max-interval 2`` bounds the beat loop so a schedule created after
        # beat starts is picked up within ~2s rather than the 60s conf default.
        return [["beat", "--max-interval", "2"]]

    def task_timeout_env(self, seconds: int) -> dict[str, str]:
        return {"CELERY_TASK_TIMEOUT": str(seconds)}

    @staticmethod
    def _require_broker(res: StackResources) -> str:
        if res.broker_url is None:
            raise RuntimeError("celery variant requires a per-stack broker_url; allocate_broker must run first")
        return res.broker_url


# ---- identity axis ------------------------------------------------------


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
# ACCESS_CONTROL_STORE Postgres database. The seed writes the provider's wire
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


# ---- storage axis -------------------------------------------------------


class StorageVariant(abc.ABC):
    """One Storage plugin: the manifest ``storage_module`` string, its feature env
    pointing at the stack's isolated storage root, and a variant-owned read-back —
    the on-disk path it stores an object at and a direct decode of that object's
    stored bytes, so a storage-variant spec asserts the plugin really wrote the
    filesystem WITHOUT going through the plugin under test."""

    name: str
    module: str
    # The provider class + defining module the ``/api/storage`` identity door
    # reports for this backend (the door names the REGISTERED class, which lives in
    # the plugin's implementation module, not necessarily its package root).
    provider_class: str
    provider_module: str

    @abc.abstractmethod
    def feature_env(self, res: StackResources) -> dict[str, str]:
        """The storage plugin's env group, pointed at this stack's resources."""

    @abc.abstractmethod
    def stored_object_path(self, storage_root: str, rel_path: str) -> Path:
        """The absolute on-disk path this backend stores ``rel_path`` at — the file
        a spec asserts the plugin created (and delete removed). Distinct per
        backend, so a read-back keyed on the wrong variant's layout finds nothing."""

    @abc.abstractmethod
    def read_stored(self, storage_root: str, rel_path: str) -> str:
        """Read the object the plugin stored at ``rel_path`` DIRECTLY off disk (not
        through the plugin), decoding this backend's on-disk format back to the
        logical content — the proof the plugin wrote the real bytes."""


class LocalStorage(StorageVariant):
    name = "local"
    module = "tai42_storage_local"
    provider_class = "LocalStorage"
    provider_module = "tai42_storage_local.storage"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        return {"STORAGE_LOCAL_ROOT_PATH": res.storage_root}

    def stored_object_path(self, storage_root: str, rel_path: str) -> Path:
        # tai42-storage-local writes each object as raw bytes at ``<root>/<path>``.
        return Path(storage_root) / rel_path

    def read_stored(self, storage_root: str, rel_path: str) -> str:
        return self.stored_object_path(storage_root, rel_path).read_text(encoding="utf-8")


# The fixture storage backend's on-disk format: every object lives under an
# ``objects/`` subtree with a byte header stamped ahead of the content, so both the
# directory shape and the leading bytes differ from the local backend's raw layout.
# The read-back mirrors what ``tai42_e2e_fixtures.storage`` writes.
_FIXTURE_STORAGE_SUBDIR = "objects"
_FIXTURE_STORAGE_HEADER = b"E2E-FIXTURE-STORAGE-V1\n"


class FixtureStorage(StorageVariant):
    """The fixture filesystem storage backend — storage provider #2, with a
    deliberately distinct on-disk layout so the storage-axis switch is proven."""

    name = "fixture"
    module = "tai42_e2e_fixtures.storage"
    provider_class = "FixtureStorage"
    provider_module = "tai42_e2e_fixtures.storage"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        return {"E2E_FIXTURE_STORAGE_ROOT_PATH": res.storage_root}

    def stored_object_path(self, storage_root: str, rel_path: str) -> Path:
        return Path(storage_root) / _FIXTURE_STORAGE_SUBDIR / rel_path

    def read_stored(self, storage_root: str, rel_path: str) -> str:
        data = self.stored_object_path(storage_root, rel_path).read_bytes()
        if not data.startswith(_FIXTURE_STORAGE_HEADER):
            raise ValueError(f"stored object {rel_path} is not in the fixture-storage format")
        return data[len(_FIXTURE_STORAGE_HEADER) :].decode("utf-8")


# ---- registries + resolver ----------------------------------------------


BACKENDS: dict[str, BackendVariant] = {"arq": ArqVariant(), "celery": CeleryVariant(), "rq": RqVariant()}
IDENTITIES: dict[str, IdentityVariant] = {"redis": RedisIdentity(), "fixture": FixtureIdentity()}
STORAGES: dict[str, StorageVariant] = {"local": LocalStorage(), "fixture": FixtureStorage()}


@dataclass(frozen=True)
class Variants:
    """The one variant set a pytest process runs under."""

    backend: BackendVariant
    identity: IdentityVariant
    storage: StorageVariant


def _resolve[T](registry: dict[str, T], name: str, env_var: str) -> T:
    try:
        return registry[name]
    except KeyError:
        valid = ", ".join(sorted(registry))
        raise InfraUnavailable(f"{env_var}={name!r} is not a known variant; valid values: {valid}") from None


def resolve_variants(settings: HarnessSettings) -> Variants:
    """Resolve the backend/identity/storage triple from the selection settings.
    An unknown name raises :class:`InfraUnavailable` naming the valid values —
    surfaced at session start through the ``tests/conftest.py::infra`` exit
    path, never silently defaulted."""
    return Variants(
        backend=_resolve(BACKENDS, settings.backend, "TAI_E2E_BACKEND"),
        identity=_resolve(IDENTITIES, settings.identity, "TAI_E2E_IDENTITY"),
        storage=_resolve(STORAGES, settings.storage, "TAI_E2E_STORAGE"),
    )
