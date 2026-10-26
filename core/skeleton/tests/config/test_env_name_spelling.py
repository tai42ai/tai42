"""Every env-name test of the config service reads a name as the settings layer does.

The settings layer reads env names case-insensitively, and a nested settings group as one JSON
value under its own name, so ``app_args_port`` sets ``AppArgsSettings.port`` and ``TAI_BUS_REDIS``
sets the bus Redis URL. The X-band, key-material, admin-pair and bus refusals, the reload
classification and the generated secret key name each treat such a name as the field it sets.
"""

from __future__ import annotations

import os

import pytest

# Import the core settings modules so their groups register.
import tai42_skeleton.app.bus_settings
import tai42_skeleton.connectors.settings
import tai42_skeleton.settings.settings  # noqa: F401
from tai42_skeleton.config.boundary import (
    EnvKeyClasses,
    refuse_incomplete_admin_pair,
    refuse_key_material,
    refuse_unrecyclable_env_change,
    refuse_x_band,
)
from tai42_skeleton.config.deployment_env import DeploymentOwnership
from tai42_skeleton.operations.manifest.secret_env import _resolve_secret_env_key

from .._helpers import recapture_deployment_env
from .fake_pipeline import (
    FakeConfigStore,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
)


@pytest.fixture(autouse=True)
def _no_deployment_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("APP_ARGS_PORT", "TAI_BUS_REDIS_MAX_CONNECTIONS", "TAI_BUS_REDIS", "CONNECTORS_KEK"):
        monkeypatch.delenv(key, raising=False)
    recapture_deployment_env()


def test_the_x_band_refusal_refuses_a_lower_case_spelling_of_an_excluded_key() -> None:
    with pytest.raises(ValueError, match="app_args_port"):
        refuse_x_band(["app_args_port"])


async def test_the_merge_write_refuses_a_lower_case_spelling_of_an_excluded_key() -> None:
    store = FakeConfigStore(env={})
    service, admin, bus = _service(store)

    with pytest.raises(ValueError, match="app_args_port"):
        await service.apply_env_change({"app_args_port": "9999"})

    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


def test_the_key_material_refusal_refuses_a_lower_case_spelling_of_a_key() -> None:
    with pytest.raises(ValueError, match="connectors_kek"):
        refuse_key_material({"connectors_kek": "new-kek"}, {})


def test_the_admin_pair_refusal_refuses_a_lower_case_half() -> None:
    with pytest.raises(ValueError, match="TAI_DATABASE_DEFAULT_PG_ADMIN_PASSWORD"):
        refuse_incomplete_admin_pair({"tai_database_default_pg_admin_user": "migrator"})


def test_a_lower_case_spelling_of_a_recycle_key_classes_as_recycle() -> None:
    hot, recycle = refuse_unrecyclable_env_change(
        {}, {"tai_bus_redis_max_connections": "9"}, DeploymentOwnership(), change="this env change"
    )

    assert hot == []
    assert recycle == ["tai_bus_redis_max_connections"]


def test_a_nested_group_s_json_name_classes_as_the_group_s_field() -> None:
    classes = EnvKeyClasses()

    assert classes.reload_class("TAI_BUS_REDIS") == "recycle"
    assert classes.reload_class("ACCESS_CONTROL_REDIS") == "hot"


def test_a_generated_secret_key_never_takes_a_nested_group_s_json_name() -> None:
    import tai42_skeleton.access_control.settings  # noqa: F401  — registers the access-control groups

    assert _resolve_secret_env_key(None, "access_control_redis", "v", {}) != "ACCESS_CONTROL_REDIS"


def test_a_generated_secret_key_never_takes_a_delimited_name_of_a_field() -> None:
    import tai42_skeleton.settings.rate_limit  # noqa: F401  — registers the rate-limit groups

    key = _resolve_secret_env_key(None, "tai_rate_limit_redis__redis_url", "v", {})

    assert key != "TAI_RATE_LIMIT_REDIS__REDIS_URL"


def test_a_generated_secret_key_never_takes_another_spelling_of_a_stored_key() -> None:
    assert _resolve_secret_env_key(None, "relay_token", "v", {"relay_token": "old"}) == "RELAY_TOKEN_2"


async def test_a_merge_that_adds_another_spelling_of_a_stored_name_is_refused_naming_both() -> None:
    store = FakeConfigStore(env={"SPELLING_TEST_LEVEL": "INFO"})
    service, admin, bus = _service(store)

    with pytest.raises(ValueError, match=r"SPELLING_TEST_LEVEL.*spelling_test_level"):
        await service.apply_env_change({"spelling_test_level": "DEBUG"})

    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_a_merge_that_renames_a_stored_name_to_another_spelling_is_accepted() -> None:
    store = FakeConfigStore(env={"SPELLING_TEST_LEVEL": "INFO"})
    service, _admin, _bus = _service(store)

    await service.apply_env_change({"SPELLING_TEST_LEVEL": "", "spelling_test_level": "DEBUG"})

    assert store.env_writes == [{"SPELLING_TEST_LEVEL": "", "spelling_test_level": "DEBUG"}]


async def test_a_merge_that_removes_one_of_two_stored_spellings_of_a_name_is_accepted() -> None:
    store = FakeConfigStore(env={"SPELLING_TEST_LEVEL": "INFO", "spelling_test_level": "DEBUG"})
    service, _admin, _bus = _service(store)

    await service.apply_env_change({"spelling_test_level": ""})

    assert store.env_writes == [{"spelling_test_level": ""}]


async def test_a_profile_that_sets_one_name_under_two_spellings_is_refused_naming_both() -> None:
    store = FakeConfigStore(env={})
    service, _admin, _bus = _service(store)

    async def save_previous(_env: dict[str, str]) -> None:
        raise AssertionError("a refused profile snapshots nothing")

    with pytest.raises(ValueError, match=r"SPELLING_TEST_LEVEL.*spelling_test_level"):
        await service.apply_replace_env(
            {"SPELLING_TEST_LEVEL": "INFO", "spelling_test_level": "DEBUG"}, save_previous=save_previous
        )

    assert store.env_writes == []


def test_the_stored_env_apply_refuses_one_name_under_two_spellings() -> None:
    from tai42_skeleton.app import applied_env

    before = dict(os.environ)
    with pytest.raises(ValueError, match=r"SPELLING_TEST_LEVEL.*spelling_test_level"):
        applied_env._apply_env({"SPELLING_TEST_LEVEL": "INFO", "spelling_test_level": "DEBUG"}, DeploymentOwnership())

    assert dict(os.environ) == before
