"""The env-write boundary classes a key by every settings field it sets.

A group's JSON name sets every field of the nested group, and in a group with a nested
delimiter a delimited name sets the field it names. The reload class, the X band and the
key-material refusal judge such a name by the fields it sets, as they judge a field's own names;
a stored change to such a name is a drift the respawn check sees.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import ClassVar

import pytest
from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import (
    KeyMaterial,
    ReloadClass,
    TaiBaseSettings,
    bind_respawn_class_env,
    unbind_respawn_class_env,
)

from tai42_skeleton.app.lifecycle.config_reload import awaiting_respawn
from tai42_skeleton.config.boundary import (
    refuse_key_material,
    refuse_unrecyclable_env_change,
    refuse_x_band,
)
from tai42_skeleton.config.deployment_env import DeploymentOwnership


class _ReachPoolSettings(TaiBaseSettings):
    """A recycle-class group nested in a hot one: its fields change only by a respawn."""

    model_config = SettingsConfigDict(env_prefix="BOUNDARY_REACH_TEST_POOL_")

    reload_class: ClassVar[ReloadClass] = "recycle"

    size: int = 1


class _ReachBootSettings(TaiBaseSettings):
    """A nested group holding a launcher-set field and a key-material field."""

    model_config = SettingsConfigDict(env_prefix="BOUNDARY_REACH_TEST_BOOT_")

    path: str = Field(default="", json_schema_extra={"reload": "excluded"})
    kek: KeyMaterial | None = None


class _ReachSettings(TaiBaseSettings):
    """A hot group with a nested delimiter that nests both groups above."""

    model_config = SettingsConfigDict(env_prefix="BOUNDARY_REACH_TEST_", env_nested_delimiter="__")

    pool: _ReachPoolSettings = Field(default_factory=_ReachPoolSettings)
    boot: _ReachBootSettings = Field(default_factory=_ReachBootSettings)


@pytest.fixture(autouse=True)
def _no_reach_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.upper().startswith("BOUNDARY_REACH_TEST_"):
            monkeypatch.delenv(key)


@pytest.mark.parametrize("key", ["BOUNDARY_REACH_TEST_POOL", "BOUNDARY_REACH_TEST_POOL__SIZE"])
def test_a_name_that_sets_a_recycle_class_field_classes_as_recycle(key: str) -> None:
    value = json.dumps({"size": 4}) if key.endswith("POOL") else "4"

    hot, recycle = refuse_unrecyclable_env_change({}, {key: value}, DeploymentOwnership(), change="this env change")

    assert (hot, recycle) == ([], [key])


@pytest.mark.parametrize("key", ["BOUNDARY_REACH_TEST_BOOT", "boundary_reach_test_boot__path"])
def test_a_name_that_sets_an_excluded_field_is_x_band(key: str) -> None:
    with pytest.raises(ValueError, match=key):
        refuse_x_band([key])


@pytest.mark.parametrize("key", ["BOUNDARY_REACH_TEST_BOOT", "BOUNDARY_REACH_TEST_BOOT__KEK"])
def test_a_name_that_sets_a_key_material_field_is_refused_a_change(key: str) -> None:
    with pytest.raises(ValueError, match=key):
        refuse_key_material({key: "new"}, {})


@pytest.fixture
def bound() -> Iterator[None]:
    bind_respawn_class_env()
    yield
    unbind_respawn_class_env()


@pytest.mark.parametrize(
    ("key", "value"),
    [("BOUNDARY_REACH_TEST_POOL", json.dumps({"size": 4})), ("BOUNDARY_REACH_TEST_POOL__SIZE", "4")],
)
def test_a_change_under_a_name_that_sets_a_bound_field_drifts(
    bound: None, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)

    assert awaiting_respawn() == ["BOUNDARY_REACH_TEST_POOL_SIZE"]
