"""The deployment env: captured once per process, read loudly, and the keys the deployment owns."""

from __future__ import annotations

import pytest
from pydantic import AliasChoices, Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings

from tai42_skeleton.config.deployment_env import (
    DeploymentOwnership,
    capture_deployment_env,
    deployment_env_keys,
    deployment_env_value,
    reset_deployment_env,
)


class _AliasedGroup(TaiBaseSettings):
    """A settings group with one field read under two env names."""

    model_config = SettingsConfigDict(env_prefix="DEPLOYMENT_ENV_TEST_")

    endpoint: str | None = Field(
        default=None, validation_alias=AliasChoices("DEPLOYMENT_ENV_TEST_ENDPOINT", "DEPLOYMENT_ENV_TEST_URL")
    )


def test_an_uncaptured_deployment_env_raises() -> None:
    reset_deployment_env()

    with pytest.raises(RuntimeError, match="deployment env is not captured"):
        deployment_env_keys()
    with pytest.raises(RuntimeError, match="deployment env is not captured"):
        DeploymentOwnership()


def test_the_capture_is_taken_once(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_deployment_env()
    monkeypatch.setenv("DEPLOYMENT_ENV_TEST_FIRST", "1")
    capture_deployment_env()
    monkeypatch.setenv("DEPLOYMENT_ENV_TEST_LATER", "2")
    capture_deployment_env()

    assert "DEPLOYMENT_ENV_TEST_FIRST" in deployment_env_keys()
    assert "DEPLOYMENT_ENV_TEST_LATER" not in deployment_env_keys()


def test_an_empty_container_value_is_not_set_by_the_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_deployment_env()
    monkeypatch.setenv("DEPLOYMENT_ENV_TEST_EMPTY", "")
    capture_deployment_env()

    assert "DEPLOYMENT_ENV_TEST_EMPTY" not in deployment_env_keys()
    assert not DeploymentOwnership().owns("DEPLOYMENT_ENV_TEST_EMPTY")
    # The empty value itself is still what the deployment carried.
    assert deployment_env_value("DEPLOYMENT_ENV_TEST_EMPTY") == ""


def test_a_settings_field_set_under_one_name_is_owned_under_every_accepted_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_deployment_env()
    monkeypatch.setenv("DEPLOYMENT_ENV_TEST_URL", "https://deployment.invalid")
    capture_deployment_env()
    ownership = DeploymentOwnership()

    assert ownership.owns("DEPLOYMENT_ENV_TEST_URL")
    assert ownership.owns("DEPLOYMENT_ENV_TEST_ENDPOINT")


def test_a_settings_name_is_compared_as_the_settings_layer_reads_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # The settings layer reads env names case-insensitively: a lower-cased deployment name sets
    # the field, so its upper-cased stored copy is owned too.
    reset_deployment_env()
    monkeypatch.setenv("deployment_env_test_endpoint", "https://deployment.invalid")
    capture_deployment_env()
    ownership = DeploymentOwnership()

    assert ownership.owns("DEPLOYMENT_ENV_TEST_ENDPOINT")
    assert ownership.owns("DEPLOYMENT_ENV_TEST_URL")


def test_changed_writes_count_only_a_set_to_a_new_value(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_deployment_env()
    monkeypatch.setenv("DEPLOYMENT_ENV_TEST_A", "deployed")
    monkeypatch.setenv("DEPLOYMENT_ENV_TEST_B", "deployed")
    monkeypatch.setenv("DEPLOYMENT_ENV_TEST_C", "deployed")
    capture_deployment_env()
    stored = {"DEPLOYMENT_ENV_TEST_B": "kept", "DEPLOYMENT_ENV_TEST_C": "old"}

    changed = DeploymentOwnership().changed_writes(
        {
            "DEPLOYMENT_ENV_TEST_A": "new",  # added
            "DEPLOYMENT_ENV_TEST_B": "kept",  # unchanged carry
            "DEPLOYMENT_ENV_TEST_C": "",  # removal of the stored copy
            "NOT_SET_BY_THE_DEPLOYMENT": "x",
        },
        stored,
    )

    assert changed == ["DEPLOYMENT_ENV_TEST_A"]
