"""Harness self-test: where a stack's processes find the worker bus URL.

By default every process of a bus stack is started with the bus URL and namespace, and the
stored ``.env`` carries neither. A ``bus_url_stored`` stack starts no process with the URL and
seeds it into the stored env instead; the namespace stays in every process env.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tai42_e2e import StackResources
from tai42_e2e.child_env import bus_env, stored_bus_env
from tai42_e2e.topology import StackConfig, Topology

pytestmark = pytest.mark.needs("no-stack")

_BUS_URL = "redis://127.0.0.1:6379/3"
_NAMESPACE = "tai42_e2e_abc123"


def _resources() -> StackResources:
    return StackResources(
        redis_idx=3,
        redis_url=_BUS_URL,
        probe_redis_url="redis://127.0.0.1:6379/0",
        pg_host="127.0.0.1",
        pg_port=5432,
        pg_user="postgres",
        pg_password="postgres",
        pg_db="tai42_e2e_probe",
        storage_root="/tmp/e2e-storage",
        bus_namespace=_NAMESPACE,
        bus_redis_url=_BUS_URL,
    )


def _config(**fields: object) -> StackConfig:
    base = StackConfig(name="bus", topology=Topology.MULTIWORKER, manifest={}, env={}, run_backend=True)
    return replace(base, **fields)


def test_a_bus_stack_starts_every_process_with_the_bus_url_and_stores_none() -> None:
    config = _config()

    assert bus_env(config, _resources()) == {"TAI_BUS_REDIS_URL": _BUS_URL, "TAI_BUS_NAMESPACE": _NAMESPACE}
    assert stored_bus_env(config, _resources()) == {}


def test_a_bus_url_stored_stack_starts_no_process_with_the_url_and_stores_it() -> None:
    config = _config(bus_url_stored=True)

    assert bus_env(config, _resources()) == {"TAI_BUS_NAMESPACE": _NAMESPACE}
    assert stored_bus_env(config, _resources()) == {"TAI_BUS_REDIS_URL": _BUS_URL}


def test_a_busless_stack_stores_no_bus_url() -> None:
    config = _config(bus_url_stored=True, workers=1, run_backend=False)

    assert bus_env(config, _resources()) == {}
    assert stored_bus_env(config, _resources()) == {}
