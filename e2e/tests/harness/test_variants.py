"""Harness self-tests for the variant adapter layer: the resolver fails loudly
on an unknown selection naming the valid values, each adapter reports its plugin
module + feature env + process model, and the backend-independent bus presence
TTL env is named on ``TAI_BUS_HEARTBEAT_TTL``."""

from __future__ import annotations

import json

import pytest

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import InfraUnavailable, StackResources
from tai42_e2e.variants import (
    BACKENDS,
    IDENTITIES,
    STORAGES,
    FixtureStorage,
    LocalStorage,
    resolve_variants,
    short_presence_ttl_env,
)

_RES = StackResources(
    redis_idx=1,
    redis_url="redis://127.0.0.1:6379/1",
    probe_redis_url="redis://127.0.0.1:6379/0",
    pg_host="127.0.0.1",
    pg_port=5432,
    pg_user="postgres",
    pg_password="postgres",
    pg_db="tai42_e2e_probe",
    storage_root="/tmp/e2e-storage",
    broker_url="amqp://guest:guest@127.0.0.1:5672/tai42_e2e_probe",
)


def test_default_selection_resolves_to_the_arq_redis_local_triple() -> None:
    # The field defaults — a bare ``pytest`` with no ``TAI_E2E_`` selection — are
    # the arq/redis/local triple; resolving them explicitly keeps the check
    # independent of the ambient matrix-leg env (which sets ``TAI_E2E_BACKEND``).
    fields = HarnessSettings.model_fields
    assert (fields["backend"].default, fields["identity"].default, fields["storage"].default) == (
        "arq",
        "redis",
        "local",
    )
    variants = resolve_variants(HarnessSettings(backend="arq", identity="redis", storage="local"))
    assert variants.backend.name == "arq"
    assert variants.backend.module == "tai42_backend_arq"
    assert variants.identity.name == "redis"
    assert variants.identity.lifecycle_module == "tai42_identity_redis"
    assert variants.storage.name == "local"
    assert variants.storage.module == "tai42_storage_local"


@pytest.mark.parametrize(
    ("settings", "env_var", "valid_name"),
    [
        (HarnessSettings(backend="nope"), "TAI_E2E_BACKEND", "arq"),
        (HarnessSettings(identity="nope"), "TAI_E2E_IDENTITY", "redis"),
        (HarnessSettings(storage="nope"), "TAI_E2E_STORAGE", "local"),
    ],
)
def test_resolve_variants_unknown_name_raises_naming_valid_values(
    settings: HarnessSettings, env_var: str, valid_name: str
) -> None:
    with pytest.raises(InfraUnavailable) as exc:
        resolve_variants(settings)
    message = str(exc.value)
    assert env_var in message
    assert "nope" in message
    # The message names the valid values so the operator can fix the selection.
    assert valid_name in message


def test_identity_and_storage_registries_carry_a_second_value() -> None:
    # The identity and storage axes are real registries, not singletons: the fixture
    # provider on each is the second value that proves the switch actually switches.
    fixture_identity = IDENTITIES["fixture"]
    assert fixture_identity.lifecycle_module == "tai42_e2e_fixtures.identity_provider"
    assert fixture_identity.auth_provider_env() == {"ACCESS_CONTROL_AUTH_PROVIDERS": json.dumps(["fixture"])}

    fixture_storage = STORAGES["fixture"]
    assert fixture_storage.module == "tai42_e2e_fixtures.storage"
    assert fixture_storage.feature_env(_RES) == {"E2E_FIXTURE_STORAGE_ROOT_PATH": _RES.storage_root}

    # The two filesystem backends store an object at DIFFERENT on-disk paths, so a
    # read-back keyed on the wrong variant's layout finds nothing — the property the
    # storage-variant spec's on-disk assertion rests on. ``_stored_object_path`` is the
    # filesystem-only helper (not the store-agnostic seam), so the registry values are
    # narrowed to their concrete filesystem classes to read it.
    local = STORAGES["local"]
    assert isinstance(local, LocalStorage)
    assert isinstance(fixture_storage, FixtureStorage)
    local_path = local._stored_object_path(_RES.storage_root, "a/b.txt")
    fixture_path = fixture_storage._stored_object_path(_RES.storage_root, "a/b.txt")
    assert local_path != fixture_path


def test_bus_presence_ttl_env_is_backend_independent() -> None:
    # Presence + its TTL live on the app-owned worker bus, not in any plugin, so the
    # dead-worker TTL env is the SAME for every backend — a single module function
    # naming ``TAI_BUS_HEARTBEAT_TTL``, not a per-backend method.
    assert short_presence_ttl_env(3) == {"TAI_BUS_HEARTBEAT_TTL": "3"}


def test_arq_backend_feature_env_and_process_model() -> None:
    # The arq backend points its plugin env at the stack's Redis.
    arq = BACKENDS["arq"]
    assert arq.feature_env(_RES) == {"ARQ_REDIS_URL": _RES.redis_url}
    assert STORAGES["local"].feature_env(_RES) == {"STORAGE_LOCAL_ROOT_PATH": _RES.storage_root}
    # arq's recurring scheduler is a self-rescheduling queue job in the worker, so
    # a schedule fires without any extra backend process.
    assert arq.extra_backend_processes() == []
    assert arq.task_timeout_env(8) == {"ARQ_TASK_TIMEOUT": "8"}


def test_rq_backend_feature_env_and_process_model() -> None:
    # The rq backend rides the stack's Redis.
    rq = BACKENDS["rq"]
    assert rq.feature_env(_RES) == {"RQ_REDIS_URL": _RES.redis_url}
    # rq-scheduler's recurring zset is only drained by a separate ``rqscheduler``
    # daemon — ``tai backend beat`` — so a schedule needs its own process.
    assert rq.extra_backend_processes() == [["beat", "-i", "1"]]
    assert rq.task_timeout_env(8) == {"RQ_TASK_TIMEOUT": "8"}


def test_celery_feature_env_and_process_model() -> None:
    celery = BACKENDS["celery"]
    assert celery.feature_env(_RES) == {
        "CELERY_BROKER_URL": _RES.broker_url,
        "CELERY_RESULT_BACKEND": _RES.redis_url,
        "CELERY_REDBEAT_REDIS_URL": _RES.redis_url,
    }
    # RedBeat only fires while a beat process runs; ``tai backend beat`` starts it.
    assert celery.extra_backend_processes() == [["beat", "--max-interval", "2"]]
    assert celery.task_timeout_env(8) == {"CELERY_TASK_TIMEOUT": "8"}
