"""Which registered settings fields an env name sets, and the value an env gives a field.

Both as pydantic-settings reads env names.
"""

import json

import pytest
from pydantic import AliasChoices, Field
from pydantic_settings import SettingsConfigDict

from tai42_kit.settings import (
    SettingsFieldRef,
    TaiBaseSettings,
    registered_settings,
    settings_field_env_value,
    settings_fields_set_by,
)
from tai42_kit.settings.registry import _REGISTRY, _clear_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Run every test on an empty registry, then restore the registrations it replaced."""
    saved = dict(_REGISTRY)
    _clear_registry()
    yield
    _clear_registry()
    _REGISTRY.update(saved)


def _groups() -> tuple[type[TaiBaseSettings], type[TaiBaseSettings]]:
    """A connection group nested in a group with a nested delimiter and an object field."""

    class NamesConnSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="NAMES_CONN_")

        url: str = ""
        timeout: int = Field(default=5, validation_alias=AliasChoices("NAMES_CONN_TIMEOUT", "NAMES_WAIT"))

    class NamesSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="NAMES_", env_nested_delimiter="__")

        conn: NamesConnSettings = Field(default_factory=NamesConnSettings)
        families: dict[str, dict[str, int]] = Field(default_factory=dict)
        level: str = "INFO"

    return NamesConnSettings, NamesSettings


def _ref(cls: type[TaiBaseSettings], field: str) -> SettingsFieldRef:
    return SettingsFieldRef(f"{cls.__module__}.{cls.__qualname__}", field)


def _info(cls: type[TaiBaseSettings], field: str):
    qualname = f"{cls.__module__}.{cls.__qualname__}"
    info = next(info for info in registered_settings() if info.qualname == qualname)
    return qualname, next(f for f in info.fields if f.name == field)


def test_a_field_s_own_names_set_it_under_every_spelling():
    conn, _parent = _groups()

    assert settings_fields_set_by("names_conn_url") == {_ref(conn, "url")}
    assert settings_fields_set_by("NAMES_WAIT") == {_ref(conn, "timeout")}


def test_a_nested_group_s_json_name_sets_every_field_of_the_group():
    conn, _parent = _groups()

    assert settings_fields_set_by("NAMES_CONN") == {_ref(conn, "url"), _ref(conn, "timeout")}


def test_a_delimited_name_sets_the_field_its_path_names():
    conn, parent = _groups()

    assert settings_fields_set_by("NAMES_CONN__URL") == {_ref(conn, "url")}
    assert settings_fields_set_by("names_conn__names_wait") == {_ref(conn, "timeout")}
    assert settings_fields_set_by("NAMES_FAMILIES__EVENTS__LIMIT") == {_ref(parent, "families")}


def test_a_name_that_sets_no_field_sets_nothing():
    _groups()

    assert settings_fields_set_by("NAMES_CONN__NOPE") == frozenset()
    assert settings_fields_set_by("NAMES_LEVEL__X") == frozenset()
    assert settings_fields_set_by("OTHER_URL") == frozenset()


@pytest.mark.parametrize(
    "env",
    [
        {"NAMES_CONN_URL": "own"},
        {"NAMES_CONN_URL": "own", "NAMES_CONN": json.dumps({"url": "json"})},
        {"NAMES_CONN": json.dumps({"url": "json"}), "NAMES_CONN__URL": "delimited"},
        {"names_conn__url": "delimited"},
        {"NAMES_CONN_URL": ""},
    ],
)
def test_a_field_s_value_is_the_one_the_settings_layer_reads(monkeypatch, env):
    conn, parent = _groups()
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    read = parent().model_dump()["conn"]["url"]

    assert settings_field_env_value(env, *_info(conn, "url")) == (read or None)


def test_an_object_field_s_value_merges_its_json_and_delimited_names(monkeypatch):
    _conn, parent = _groups()
    env = {"NAMES_FAMILIES": json.dumps({"events": {"limit": 5, "window": 60}}), "NAMES_FAMILIES__EVENTS__LIMIT": "9"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    value = settings_field_env_value(env, *_info(parent, "families"))

    # A delimited name's value is the string it is set to; the field's validation coerces it.
    assert value is not None
    assert json.loads(value) == {"events": {"limit": "9", "window": 60}}
    assert parent().model_dump()["families"] == {"events": {"limit": 9, "window": 60}}


def test_a_name_whose_value_must_be_json_and_is_not_raises():
    conn, _parent = _groups()

    with pytest.raises(ValueError, match="NAMES_CONN"):
        settings_field_env_value({"NAMES_CONN": "not json"}, *_info(conn, "url"))


def test_a_nested_group_reference_holds_no_value_of_its_own():
    _conn, parent = _groups()

    with pytest.raises(ValueError, match="references a nested group"):
        settings_field_env_value({}, *_info(parent, "conn"))
