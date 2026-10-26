"""Recycle refusal policy: the bus-reaching-key refusal, the census kinds, and the X-classification of the
deployment-infra bare reads."""

from __future__ import annotations

import pytest

from tai42_skeleton.app.bus import WorkerKind
from tai42_skeleton.app.bus_settings import fleet_env_names
from tai42_skeleton.config.recycle_policy import (
    CENSUS_TARGET_KINDS,
    X_CLASSIFIED_DEPLOYMENT_BARE_READS,
    _refuse_unrecyclable,
    bus_reaching_keys,
)


def test_the_bus_reaching_keys_are_the_bus_urls_and_the_bus_namespace_under_any_spelling() -> None:
    keys = {"TAI_BUS_REDIS_URL", "tai_default_redis_url", "TAI_BUS_REDIS", "Tai_Bus_Namespace", "LOG_LEVEL"}
    expected = ["TAI_BUS_REDIS", "TAI_BUS_REDIS_URL", "Tai_Bus_Namespace", "tai_default_redis_url"]
    assert bus_reaching_keys(keys) == expected


def test_census_target_kinds_are_backend_then_serve() -> None:
    assert (WorkerKind.backend, WorkerKind.serve) == CENSUS_TARGET_KINDS


def test_deployment_bare_reads_are_x_classified() -> None:
    # A profile can NEVER carry these; the boundary validator folds this set into its
    # X-band refusal enforced at every env writer.
    assert frozenset({"TAI_READY_SENTINEL_PATH"}) == X_CLASSIFIED_DEPLOYMENT_BARE_READS


@pytest.mark.parametrize("key", sorted(fleet_env_names()))
def test_a_bus_url_in_the_diff_is_refused_with_the_bus_reason(key: str) -> None:
    with pytest.raises(ValueError, match="Refusing to apply this env change") as excinfo:
        _refuse_unrecyclable({key, "LOG_LEVEL"}, change="this env change")
    message = str(excinfo.value)
    assert f"bus-reaching key(s): {key}." in message
    assert "locate the worker bus and its census, which no recycle can carry" in message
    assert "deployment's environment" in message
    assert "LOG_LEVEL" not in message


def test_a_diff_without_a_bus_url_passes() -> None:
    _refuse_unrecyclable({"LOG_LEVEL", "SUB_MCP_REDIS_URL"}, change="this env change")
