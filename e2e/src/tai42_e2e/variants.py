"""The variant adapter layer: one small object per backend / identity / storage
axis, holding everything plugin-specific that would otherwise leak into
``manifests.py``, ``harness.py``, ``redisx.py``, and the reload spec.

One variant set is resolved per pytest process from the ``TAI_E2E_`` selection
env vars (``resolve_variants``); an unknown name raises loudly at session start
naming the valid values. Adding a fourth backend is one adapter class plus one
registry entry — no edits to the boot engine or the manifest builders.

The live-fleet census is backend-INDEPENDENT: it reads the app-owned worker bus
(:func:`bus_census`), a scan of the per-name presence keys that every subscribed
process — HTTP ``serve`` worker and backend runtime alike — advertises. It is a
module function, not a per-backend method, because the bus is app infrastructure
the plugin does not own. The harness never imports a system-under-test package to
read it: it scans the presence keys off the bus Redis through the ``redis`` client.
"""

from __future__ import annotations

import abc
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import redis

from tai42_e2e.rabbitx import RabbitAdmin, broker_url_for
from tai42_e2e.settings import REAL_SERVICES
from tai42_e2e.stack import Infra, InfraUnavailable, StackResources

if TYPE_CHECKING:
    from tai42_e2e.settings import HarnessSettings


# ---- app worker bus census ----------------------------------------------


@dataclass(frozen=True)
class BusWorker:
    """One live worker on the app-owned worker bus, parsed from a presence key + value:
    the slot ``name`` (``{kind}-{n}``, the lowest-free ordinal held for one claim's life)
    off the key, and the presence value's ``kind`` (``serve`` for an HTTP worker,
    ``backend`` for a runtime worker), ``pid``, ``generation`` (the monotonic life counter
    minted with the claim), lifecycle ``state`` (``ready`` / ``resyncing`` / ``recycling``),
    the ``joined_at`` / ``beat_at`` timestamps, and the optional ``last_op`` summary."""

    name: str
    kind: str
    pid: int
    generation: int
    joined_at: str
    beat_at: str
    state: str
    last_op: dict[str, Any] | None = None


def bus_census(bus_redis_url: str, namespace: str) -> list[BusWorker]:
    """The live fleet on the worker bus: scan the per-name presence keys under this
    stack's namespace on the bus Redis and parse each value's
    ``{kind, pid, generation, joined_at, beat_at, state, last_op?}``.

    Backend-independent — every subscribed worker (both the HTTP ``serve`` workers and
    the ``backend`` runtime) advertises exactly one presence key under its slot name, so
    this is the whole fleet the reload/readiness seams draw from, read straight off the
    bus Redis (the harness never imports the system under test). A key that expires
    between the scan and the value read is skipped; a malformed value raises loudly
    (a stack scans only its own namespace, so it sees only its own well-formed keys)."""
    prefix = f"{namespace}:bus:presence:"
    client = redis.Redis.from_url(bus_redis_url, decode_responses=True)
    try:
        workers: list[BusWorker] = []
        for key in client.scan_iter(match=f"{prefix}*"):
            value = client.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise TypeError(f"presence value for {key!r} is not a decoded string: {type(value)!r}")
            meta = json.loads(value)
            workers.append(
                BusWorker(
                    name=key[len(prefix) :],
                    kind=meta["kind"],
                    pid=int(meta["pid"]),
                    generation=int(meta["generation"]),
                    joined_at=meta["joined_at"],
                    beat_at=meta["beat_at"],
                    state=meta["state"],
                    last_op=meta.get("last_op"),
                )
            )
        return workers
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
        # The logical DB isolates co-tenant stacks, but a DB index re-leased while a
        # leaked worker still consumes it would let that orphan dequeue this stack's
        # ``tool_execution`` jobs off the shared default queue key. Namespacing the
        # queue by the same per-stack token as the bus keeps each worker on its own.
        return {
            "ARQ_REDIS_URL": res.redis_url,
            "ARQ_QUEUE_NAME": f"{res.bus_namespace}:arq:queue",
        }

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
        # Same reasoning as arq: namespace the RQ queue by the per-stack token so a
        # leaked worker on a re-leased DB index cannot consume this stack's jobs.
        return {
            "RQ_REDIS_URL": res.redis_url,
            "RQ_QUEUE_NAME": f"{res.bus_namespace}:default",
        }

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
        # The task queue rides a per-stack RabbitMQ vhost (``allocate_broker``), so
        # tool_execution is already isolated and reaped with the lease — no queue
        # rename is needed. Result records key on unique task ids. The one shared
        # Redis structure is RedBeat's schedule store on the logical DB, so its key
        # prefix is namespaced by the per-stack token to stop a leaked beat on a
        # re-leased DB from firing this stack's schedules.
        return {
            "CELERY_BROKER_URL": broker_url,
            "CELERY_RESULT_BACKEND": res.redis_url,
            "CELERY_REDBEAT_REDIS_URL": res.redis_url,
            "CELERY_REDBEAT_KEY_PREFIX": f"{res.bus_namespace}:redbeat:",
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


# ---- storage axis -------------------------------------------------------


class StorageVariant(abc.ABC):
    """One Storage plugin: the manifest ``storage_module`` string, its feature env
    pointing at the stack's isolated store, and a store-agnostic read-back —
    :meth:`assert_stored` / :meth:`assert_absent`, which each variant implements
    against ITS OWN store (a filesystem tree, an S3 bucket, a fake GitHub repo)
    through an independent client, never the plugin under test. A store-shaped
    (``Path``-returning) contract cannot span object stores, so the seam is the two
    assertion methods, not a path accessor."""

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
    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        """Assert the plugin really stored ``content`` at ``rel_path`` in this
        backend's own store — read back through an independent client (never the
        plugin under test), the proof the plugin wrote the real bytes. Raises
        :class:`AssertionError` naming the store location when it did not."""

    @abc.abstractmethod
    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        """Assert no object exists at ``rel_path`` in this backend's store, read
        through the same independent client — the delete-really-removed-it proof.
        Raises :class:`AssertionError` when the object is still present."""


class LocalStorage(StorageVariant):
    name = "local"
    module = "tai42_storage_local"
    provider_class = "LocalStorage"
    provider_module = "tai42_storage_local.storage"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        return {"STORAGE_LOCAL_ROOT_PATH": res.storage_root}

    def _stored_object_path(self, storage_root: str, rel_path: str) -> Path:
        # tai42-storage-local writes each object as raw bytes at ``<root>/<path>``.
        # A filesystem-layout detail distinct from the fixture backend's subtree, so
        # the two variants store the same object at different paths. Private: a
        # filesystem-only helper, NOT part of the store-agnostic StorageVariant seam.
        return Path(storage_root) / rel_path

    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        path = self._stored_object_path(res.storage_root, rel_path)
        if not path.exists():
            raise AssertionError(f"local storage plugin did not write {path}")
        actual = path.read_text(encoding="utf-8")
        if actual != content:
            raise AssertionError(f"stored bytes at {path} decode to {actual!r}, not {content!r}")

    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        path = self._stored_object_path(res.storage_root, rel_path)
        if path.exists():
            raise AssertionError(f"object still present on disk: {path}")


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

    def _stored_object_path(self, storage_root: str, rel_path: str) -> Path:
        # Private: a filesystem-only helper, NOT part of the store-agnostic seam.
        return Path(storage_root) / _FIXTURE_STORAGE_SUBDIR / rel_path

    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        path = self._stored_object_path(res.storage_root, rel_path)
        if not path.exists():
            raise AssertionError(f"fixture storage plugin did not write {path}")
        data = path.read_bytes()
        if not data.startswith(_FIXTURE_STORAGE_HEADER):
            raise AssertionError(f"stored object {path} is not in the fixture-storage format")
        actual = data[len(_FIXTURE_STORAGE_HEADER) :].decode("utf-8")
        if actual != content:
            raise AssertionError(f"stored bytes at {path} decode to {actual!r}, not {content!r}")

    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        path = self._stored_object_path(res.storage_root, rel_path)
        if path.exists():
            raise AssertionError(f"object still present on disk: {path}")


# ---- s3 + github hermetic-leg coordinates -------------------------------
# The s3 and github storage legs run against harness-owned stand-ins — a
# storage-profile MinIO container (``compose.yml`` ``storage`` profile) and an
# in-process fake GitHub REST server (``tests/storage`` fixture) — NEVER a real
# vendor. Both the SUT env (:meth:`feature_env`) and the variant read-back read the
# stand-in's coordinates from the SAME env vars, so a leg wires one endpoint and
# both ends agree without threading the value through per-stack resources.

# One shared bucket for the whole s3 leg; the fixture creates it, every stack in
# the leg stores under it, and object keys are per-test unique (``uniq``).
S3_AXIS_BUCKET = "tai42-e2e-storage"
_S3_ENDPOINT_ENV = "TAI_E2E_S3_ENDPOINT"
_S3_ACCESS_KEY_ENV = "TAI_E2E_S3_ACCESS_KEY"
_S3_SECRET_KEY_ENV = "TAI_E2E_S3_SECRET_KEY"
# Defaults match the storage-profile MinIO service in ``compose.yml``.
_S3_DEFAULT_ENDPOINT = "http://127.0.0.1:9002"
_S3_DEFAULT_ACCESS_KEY = "minio"
_S3_DEFAULT_SECRET_KEY = "miniosecret"

# The fake GitHub REST server's origin, shared by the SUT env and the read-back.
# ``storage_axis_backing`` publishes an allocated free port on ``GITHUB_STUB_ENV``
# before the stacks render, which ``feature_env`` then reads; an explicit pin wins,
# and this default is only the last-resort fallback when neither is set.
GITHUB_STUB_ENV = "TAI_E2E_GITHUB_STUB_BASE"
GITHUB_STUB_DEFAULT = "http://127.0.0.1:9099"
# The single repo the fake GitHub server keys objects under; constants both ends share.
_GITHUB_USERNAME = "tai42-e2e"
_GITHUB_REPO = "storage"
_GITHUB_BRANCH = "main"


@dataclass(frozen=True)
class S3Coordinates:
    """The MinIO endpoint + credentials + axis bucket the s3 leg shares between the
    SUT env and the independent read-back client."""

    endpoint: str
    access_key: str
    secret_key: str
    bucket: str


def s3_coordinates() -> S3Coordinates:
    """The s3 leg's MinIO coordinates, read from env with ``compose.yml`` defaults."""
    return S3Coordinates(
        endpoint=os.environ.get(_S3_ENDPOINT_ENV, _S3_DEFAULT_ENDPOINT),
        access_key=os.environ.get(_S3_ACCESS_KEY_ENV, _S3_DEFAULT_ACCESS_KEY),
        secret_key=os.environ.get(_S3_SECRET_KEY_ENV, _S3_DEFAULT_SECRET_KEY),
        bucket=S3_AXIS_BUCKET,
    )


def open_s3_client() -> Any:
    """An independent boto3 S3 client at the leg's MinIO coordinates — the storage
    read-back path AND the fixture's bucket-create, never the aioboto3 plugin under
    test. boto3 is imported lazily: it is an s3-leg-only dependency, absent on every
    other storage leg."""
    import boto3
    from botocore.config import Config

    coords = s3_coordinates()
    return boto3.client(
        "s3",
        endpoint_url=coords.endpoint,
        aws_access_key_id=coords.access_key,
        aws_secret_access_key=coords.secret_key,
        region_name="us-east-1",
        use_ssl=False,
        verify=False,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def github_stub_base() -> str:
    """The fake GitHub REST server's origin (SUT env + read-back share it)."""
    return os.environ.get(GITHUB_STUB_ENV, GITHUB_STUB_DEFAULT)


def _github_raw_url(rel_path: str) -> str:
    return f"{github_stub_base()}/raw/{_GITHUB_USERNAME}/{_GITHUB_REPO}/refs/heads/{_GITHUB_BRANCH}/{rel_path}"


_S3_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class S3Storage(StorageVariant):
    """The S3 storage backend, run hermetically against a storage-profile MinIO
    container (never a real AWS endpoint). Read-back goes through an independent
    boto3 client against the same bucket, never the aioboto3 plugin under test."""

    name = "s3"
    module = "tai42_storage_s3"
    provider_class = "S3Storage"
    provider_module = "tai42_storage_s3.storage"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        coords = s3_coordinates()
        # MinIO has no virtual-host buckets and serves plain HTTP in the leg, so
        # the SUT client must use path-style addressing over an insecure transport.
        return {
            "STORAGE_S3_ENDPOINT": coords.endpoint,
            "STORAGE_S3_BUCKET": coords.bucket,
            "STORAGE_S3_ACCESS_KEY": coords.access_key,
            "STORAGE_S3_SECRET_KEY": coords.secret_key,
            "STORAGE_S3_SECURE": "false",
            "STORAGE_S3_VERIFY_SSL": "false",
            "STORAGE_S3_ADDRESSING_STYLE": "path",
        }

    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        from botocore.exceptions import ClientError

        client = open_s3_client()
        try:
            try:
                resp = client.get_object(Bucket=S3_AXIS_BUCKET, Key=rel_path)
            except ClientError as exc:
                if _s3_error_code(exc) in _S3_NOT_FOUND_CODES:
                    raise AssertionError(f"S3 storage plugin did not write s3://{S3_AXIS_BUCKET}/{rel_path}") from None
                raise
            actual = resp["Body"].read().decode("utf-8")
        finally:
            client.close()
        if actual != content:
            loc = f"s3://{S3_AXIS_BUCKET}/{rel_path}"
            raise AssertionError(f"stored bytes at {loc} decode to {actual!r}, not {content!r}")

    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        from botocore.exceptions import ClientError

        client = open_s3_client()
        try:
            client.head_object(Bucket=S3_AXIS_BUCKET, Key=rel_path)
        except ClientError as exc:
            if _s3_error_code(exc) in _S3_NOT_FOUND_CODES:
                return
            raise
        finally:
            client.close()
        raise AssertionError(f"object still present: s3://{S3_AXIS_BUCKET}/{rel_path}")


def _s3_error_code(exc: Any) -> str | None:
    return exc.response.get("Error", {}).get("Code")


class GithubStorage(StorageVariant):
    """The GitHub storage backend, run hermetically against an in-process fake
    GitHub REST server (raw + contents + trees). Read-back GETs the fake's raw
    endpoint directly (an independent httpx client), never the plugin under test."""

    name = "github"
    module = "tai42_storage_github"
    provider_class = "GithubStorage"
    provider_module = "tai42_storage_github.storage"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        base = github_stub_base()
        # The base-URL settings hold ``{username}``/``{repo}``/``{branch}`` placeholders
        # the plugin ``.format()``s; only the host is swapped from the real GitHub
        # surfaces to the fake, so the plugin's URL construction is exercised unchanged.
        return {
            "STORAGE_GITHUB_USERNAME": _GITHUB_USERNAME,
            "STORAGE_GITHUB_REPO": _GITHUB_REPO,
            "STORAGE_GITHUB_BRANCH": _GITHUB_BRANCH,
            "STORAGE_GITHUB_RAW_BASE_URL": f"{base}/raw/{{username}}/{{repo}}/refs/heads/{{branch}}",
            "STORAGE_GITHUB_CONTENTS_API_URL": f"{base}/api/repos/{{username}}/{{repo}}/contents",
            "STORAGE_GITHUB_TREES_API_URL": f"{base}/api/repos/{{username}}/{{repo}}/git/trees/{{branch}}",
        }

    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        import httpx

        url = _github_raw_url(rel_path)
        resp = httpx.get(url)
        if resp.status_code == 404:
            raise AssertionError(f"GitHub storage plugin did not write {rel_path} (404 at {url})")
        resp.raise_for_status()
        if resp.text != content:
            raise AssertionError(f"stored bytes at {url} decode to {resp.text!r}, not {content!r}")

    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        import httpx

        url = _github_raw_url(rel_path)
        resp = httpx.get(url)
        if resp.status_code != 404:
            raise AssertionError(f"object still present at {url}: HTTP {resp.status_code}")


# ---- real-vendor storage siblings ---------------------------------------
# The real siblings of the hermetic ``s3`` / ``github`` legs: they talk to a LIVE
# bucket / repo from the operator-supplied ``STORAGE_*`` credentials (the same var
# names the plugin reads), NEVER a harness stand-in — the plugin's own defaults
# already point at the real vendor, so the real leg overrides nothing but the
# coordinates. Selected on the storage axis (``TAI_E2E_STORAGE=s3-real`` /
# ``github-real``); the operator ALSO names the seam on ``TAI_E2E_REAL``
# (``storage-s3`` / ``storage-github``) so the collection-time loud-fail checks the
# credentials. These are NEW siblings — the hermetic ``S3Storage`` / ``GithubStorage``
# legs above are the mock floor and are left untouched.


def _real_leg_env(service: str) -> dict[str, str]:
    """The operator-supplied env for a real storage leg, read verbatim from the
    ambient environment. A missing or empty required var (per ``REAL_SERVICES`` — the
    single source of truth) raises loudly naming the exact vars, so selecting a real
    storage variant without its credentials never boots half-configured (the same
    contract the ``TAI_E2E_REAL`` collection gate enforces)."""
    required = REAL_SERVICES[service].required_env
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise InfraUnavailable(
            f"real storage leg {service!r} needs env var(s): {', '.join(missing)} "
            f"(also select TAI_E2E_REAL={service} so they are checked at collection)"
        )
    return {key: os.environ[key] for key in required}


def _real_s3_client(env: dict[str, str]) -> Any:
    """An independent boto3 client at the operator's real bucket coordinates — the
    read-back path, never the aioboto3 plugin under test. Transport security follows
    the endpoint scheme; path addressing is the default non-AWS stores require."""
    import boto3
    from botocore.config import Config

    style = os.environ.get("STORAGE_S3_ADDRESSING_STYLE", "path")
    return boto3.client(
        "s3",
        endpoint_url=env["STORAGE_S3_ENDPOINT"],
        aws_access_key_id=env["STORAGE_S3_ACCESS_KEY"],
        aws_secret_access_key=env["STORAGE_S3_SECRET_KEY"],
        region_name=env["STORAGE_S3_REGION"],
        config=Config(signature_version="s3v4", s3={"addressing_style": style}),
    )


class S3RealStorage(StorageVariant):
    """The S3 storage backend against a LIVE bucket (``STORAGE_S3_*`` from the filled
    template). Read-back goes through an independent boto3 client at the same real
    coordinates, never the plugin under test."""

    name = "s3-real"
    module = "tai42_storage_s3"
    provider_class = "S3Storage"
    provider_module = "tai42_storage_s3.storage"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        env = _real_leg_env("storage-s3")
        # Path addressing + the narrow checksum mode default to the values non-AWS
        # S3-compatible stores (e.g. OCI) require; an operator override wins.
        env["STORAGE_S3_ADDRESSING_STYLE"] = os.environ.get("STORAGE_S3_ADDRESSING_STYLE", "path")
        env["STORAGE_S3_REQUEST_CHECKSUM_CALCULATION"] = os.environ.get(
            "STORAGE_S3_REQUEST_CHECKSUM_CALCULATION", "when_required"
        )
        return env

    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        from botocore.exceptions import ClientError

        env = _real_leg_env("storage-s3")
        bucket = env["STORAGE_S3_BUCKET"]
        client = _real_s3_client(env)
        try:
            try:
                resp = client.get_object(Bucket=bucket, Key=rel_path)
            except ClientError as exc:
                if _s3_error_code(exc) in _S3_NOT_FOUND_CODES:
                    raise AssertionError(f"S3 storage plugin did not write s3://{bucket}/{rel_path}") from None
                raise
            actual = resp["Body"].read().decode("utf-8")
        finally:
            client.close()
        if actual != content:
            raise AssertionError(f"stored bytes at s3://{bucket}/{rel_path} decode to {actual!r}, not {content!r}")

    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        from botocore.exceptions import ClientError

        env = _real_leg_env("storage-s3")
        bucket = env["STORAGE_S3_BUCKET"]
        client = _real_s3_client(env)
        try:
            client.head_object(Bucket=bucket, Key=rel_path)
        except ClientError as exc:
            if _s3_error_code(exc) in _S3_NOT_FOUND_CODES:
                return
            raise
        finally:
            client.close()
        raise AssertionError(f"object still present: s3://{bucket}/{rel_path}")


class GithubRealStorage(StorageVariant):
    """The GitHub storage backend against a LIVE repo (``STORAGE_GITHUB_*`` from the
    filled template). The plugin's base-URL settings already default to real GitHub,
    so the real leg sets only the coordinates + PAT. Read-back GETs the repo's
    Contents API with the same token (an independent httpx client, so private repos
    read back too), never the plugin under test."""

    name = "github-real"
    module = "tai42_storage_github"
    provider_class = "GithubStorage"
    provider_module = "tai42_storage_github.storage"

    def feature_env(self, res: StackResources) -> dict[str, str]:
        env = _real_leg_env("storage-github")
        # BRANCH is not in the required set (defaults ``main`` at the plugin); pass it
        # through when the operator pinned one. No RAW/CONTENTS/TREES overrides — the
        # plugin defaults already address real GitHub.
        branch = os.environ.get("STORAGE_GITHUB_BRANCH")
        if branch:
            env["STORAGE_GITHUB_BRANCH"] = branch
        return env

    def _contents_get(self, rel_path: str) -> Any:
        import httpx

        env = _real_leg_env("storage-github")
        branch = os.environ.get("STORAGE_GITHUB_BRANCH", "main")
        url = f"https://api.github.com/repos/{env['STORAGE_GITHUB_USERNAME']}/{env['STORAGE_GITHUB_REPO']}/contents/{rel_path}"
        return httpx.get(
            url,
            params={"ref": branch},
            headers={
                "Authorization": f"Bearer {env['STORAGE_GITHUB_TOKEN']}",
                "Accept": "application/vnd.github.raw+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def assert_stored(self, res: StackResources, rel_path: str, content: str) -> None:
        resp = self._contents_get(rel_path)
        if resp.status_code == 404:
            raise AssertionError(f"GitHub storage plugin did not write {rel_path} (404 at contents API)")
        resp.raise_for_status()
        if resp.text != content:
            raise AssertionError(f"stored bytes for {rel_path} decode to {resp.text!r}, not {content!r}")

    def assert_absent(self, res: StackResources, rel_path: str) -> None:
        resp = self._contents_get(rel_path)
        if resp.status_code != 404:
            raise AssertionError(f"object still present at {rel_path}: HTTP {resp.status_code}")


# ---- registries + resolver ----------------------------------------------


BACKENDS: dict[str, BackendVariant] = {"arq": ArqVariant(), "celery": CeleryVariant(), "rq": RqVariant()}
IDENTITIES: dict[str, IdentityVariant] = {"redis": RedisIdentity(), "fixture": FixtureIdentity()}
STORAGES: dict[str, StorageVariant] = {
    "local": LocalStorage(),
    "fixture": FixtureStorage(),
    "s3": S3Storage(),
    "github": GithubStorage(),
    "s3-real": S3RealStorage(),
    "github-real": GithubRealStorage(),
}


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
