"""A key the deployment sets owns every env name that sets any settings field it sets.

The settings layer sets a field under more names than the field's own accepted names: a nested
group's JSON name sets every field of the group (``HOOKS_REDIS`` sets the hooks Redis URL), and
in a group with a nested delimiter a delimited name sets the field it names
(``TAI_RATE_LIMIT_REDIS__REDIS_URL``). Every name compares folded, the names no settings group
reads included. Every door judges by this ownership: the env writes, the stored-env apply at
boot and reload, and the settings schema's lock.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app
from tai42_kit.settings import registered_settings

# Import the settings modules so their groups register.
import tai42_skeleton.app.bus_settings
import tai42_skeleton.hooks.settings
import tai42_skeleton.settings.rate_limit  # noqa: F401
from tai42_skeleton.app import applied_env, instance
from tai42_skeleton.config.deployment_env import DeploymentOwnership, published_deployment_keys
from tai42_skeleton.hooks.settings import HooksSettings
from tai42_skeleton.operations import config as config_ops

from .._fakes.bus import FakeBus
from .._helpers import recapture_deployment_env
from .fake_pipeline import (
    FakeConfigStore,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
)

_PREFIXES = ("HOOKS_", "TAI_RATE_LIMIT_", "TAI_BUS_", "OWNERSHIP_REACH_TEST_")
_HOOKS_REDIS = "tai42_skeleton.hooks.settings.HooksRedisSettings"


@pytest.fixture(autouse=True)
def _no_deployment_values(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(os.environ):
        if key.upper().startswith(_PREFIXES):
            monkeypatch.delenv(key)
    monkeypatch.setattr(applied_env, "_loaded_env_keys", set())
    yield
    # A stored-env apply writes ``os.environ`` directly; drop what it wrote before the
    # monkeypatch restores the values the test started with.
    for key in list(os.environ):
        if key.upper().startswith(_PREFIXES):
            del os.environ[key]


def _owned_after(monkeypatch: pytest.MonkeyPatch, deployment: dict[str, str]) -> DeploymentOwnership:
    for key, value in deployment.items():
        monkeypatch.setenv(key, value)
    recapture_deployment_env()
    return DeploymentOwnership()


def test_a_nested_group_s_json_name_owns_every_name_of_the_group_s_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    ownership = _owned_after(monkeypatch, {"HOOKS_REDIS": json.dumps({"redis_url": "redis://deployment"})})

    assert ownership.owns("HOOKS_REDIS_URL")
    assert ownership.owns("hooks_socket_timeout")


def test_a_nested_group_s_field_owns_the_group_s_json_name(monkeypatch: pytest.MonkeyPatch) -> None:
    ownership = _owned_after(monkeypatch, {"HOOKS_REDIS_URL": "redis://deployment"})

    assert ownership.owns("HOOKS_REDIS")
    assert ownership.owns("hooks_redis")
    assert not ownership.owns("HOOKS_SOCKET_TIMEOUT")


def test_a_delimited_name_owns_the_field_it_names_under_every_name(monkeypatch: pytest.MonkeyPatch) -> None:
    ownership = _owned_after(monkeypatch, {"TAI_RATE_LIMIT_REDIS__REDIS_URL": "redis://deployment"})

    assert ownership.owns("TAI_RATE_LIMIT_REDIS_URL")
    assert ownership.owns("TAI_RATE_LIMIT_REDIS")
    assert ownership.owns("tai_rate_limit_redis__redis_url")
    assert not ownership.owns("TAI_RATE_LIMIT_REDIS_MAX_CONNECTIONS")


def test_a_field_owns_its_delimited_names(monkeypatch: pytest.MonkeyPatch) -> None:
    ownership = _owned_after(monkeypatch, {"TAI_RATE_LIMIT_REDIS_URL": "redis://deployment"})

    assert ownership.owns("TAI_RATE_LIMIT_REDIS__REDIS_URL")
    assert not ownership.owns("TAI_RATE_LIMIT_REDIS__REDIS_MAX_CONNECTIONS")


def test_a_delimited_name_below_an_object_field_owns_the_field(monkeypatch: pytest.MonkeyPatch) -> None:
    ownership = _owned_after(monkeypatch, {"TAI_RATE_LIMIT_FAMILIES": json.dumps({"events": {"limit": 5}})})

    assert ownership.owns("TAI_RATE_LIMIT_FAMILIES__EVENTS__LIMIT")


def test_a_name_another_worker_publishes_owns_the_names_of_the_fields_it_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    recapture_deployment_env()
    ownership = DeploymentOwnership(["HOOKS_REDIS"])

    assert ownership.owns("HOOKS_REDIS_URL")


def test_a_name_no_settings_group_reads_is_owned_under_every_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reader outside the settings registry (FastMCP's own settings, a plain BaseSettings)
    # reads env names case-insensitively too.
    ownership = _owned_after(monkeypatch, {"OWNERSHIP_REACH_TEST_LEVEL": "WARNING"})

    assert ownership.owns("ownership_reach_test_level")


async def test_a_write_of_another_name_of_an_owned_field_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _owned_after(monkeypatch, {"HOOKS_REDIS": json.dumps({"redis_url": "redis://deployment"})})
    store = FakeConfigStore(env={})
    service, admin, _bus = _service(store)

    with pytest.raises(ValueError, match="HOOKS_REDIS_URL"):
        await service.apply_env_change({"HOOKS_REDIS_URL": "redis://stored"})

    assert store.env_writes == []
    assert admin.calls == 0


def test_the_stored_env_apply_keeps_the_deployment_s_value_under_another_name(monkeypatch: pytest.MonkeyPatch) -> None:
    ownership = _owned_after(monkeypatch, {"HOOKS_REDIS_URL": "redis://deployment"})

    applied_env._apply_env({"HOOKS_REDIS": json.dumps({"redis_url": "redis://stored"})}, ownership)

    assert "HOOKS_REDIS" not in os.environ
    assert HooksSettings().redis.redis_url == "redis://deployment"


def _install_schema(monkeypatch: pytest.MonkeyPatch, stored: dict[str, str]) -> None:
    manager = SimpleNamespace(read_env=lambda: dict(stored), read_manifest_preserved=dict)
    impl = SimpleNamespace(config=SimpleNamespace(config_manager=manager), admin=None, backends=None)
    monkeypatch.setattr(tai42_app, "_impl", impl)
    monkeypatch.setattr(instance.app, "_bus", FakeBus(origin="serve-x"))


def _schema_field(result: dict, qualname: str, name: str) -> dict:
    group = next(group for group in result["groups"] if group["qualname"] == qualname)
    return next(field for field in group["fields"] if field["name"] == name)


async def test_the_schema_locks_a_field_the_deployment_sets_through_its_group_s_json_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _owned_after(monkeypatch, {"HOOKS_REDIS": json.dumps({"redis_url": "redis://deployment"})})
    _install_schema(monkeypatch, {"HOOKS_REDIS_URL": "redis://stored"})

    field = _schema_field(await config_ops.read_settings_schema(), _HOOKS_REDIS, "redis_url")

    assert (field["value"], field["value_source"]) == ("redis://deployment", "deployment")


async def test_the_schema_reports_a_stored_value_set_through_its_group_s_json_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recapture_deployment_env()
    _install_schema(monkeypatch, {"HOOKS_REDIS": json.dumps({"redis_url": "redis://stored"})})

    field = _schema_field(await config_ops.read_settings_schema(), _HOOKS_REDIS, "redis_url")

    assert (field["value"], field["value_source"]) == ("redis://stored", "stored")


def _every_registered_name() -> set[str]:
    return {name for info in registered_settings() for field in info.fields for name in field.accepted_env_vars}


@pytest.mark.parametrize(
    "deployment",
    [
        {"HOOKS_REDIS_URL": "redis://deployment"},
        {"HOOKS_REDIS": json.dumps({"redis_url": "redis://deployment"})},
        {"TAI_RATE_LIMIT_REDIS_URL": "redis://deployment"},
        {"TAI_RATE_LIMIT_REDIS__REDIS_URL": "redis://deployment"},
        {"TAI_BUS_REDIS_URL": "redis://deployment"},
    ],
)
def test_the_fleet_view_of_a_worker_s_published_names_owns_what_the_worker_owns(
    monkeypatch: pytest.MonkeyPatch, deployment: dict[str, str]
) -> None:
    names = _every_registered_name()
    own = _owned_after(monkeypatch, deployment).owned(names)
    published = published_deployment_keys()
    in_own_row = DeploymentOwnership(published).owned(names)
    for key in deployment:
        monkeypatch.delenv(key)
    in_another_worker = _owned_after(monkeypatch, {})

    assert in_own_row == own
    assert not in_another_worker.owned(deployment)
    assert DeploymentOwnership(published).owned(names) == own


async def test_a_write_of_a_field_the_deployment_does_not_set_in_a_nested_group_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _owned_after(monkeypatch, {"HOOKS_REDIS_URL": "redis://deployment"})
    store = FakeConfigStore(env={})
    service, _admin, _bus = _service(store)

    await service.apply_env_change({"HOOKS_SOCKET_TIMEOUT": "9"})

    assert store.env_writes == [{"HOOKS_SOCKET_TIMEOUT": "9"}]


def test_the_census_withdraw_keeps_a_stored_field_of_a_nested_group_the_deployment_does_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership = _owned_after(monkeypatch, {"HOOKS_REDIS_URL": "redis://deployment"})
    applied_env._apply_env({"HOOKS_SOCKET_TIMEOUT": "9"}, ownership)

    withdrawn = applied_env._withdraw_owned_applied_keys(DeploymentOwnership(published_deployment_keys()))

    assert withdrawn == []
    assert os.environ["HOOKS_SOCKET_TIMEOUT"] == "9"
