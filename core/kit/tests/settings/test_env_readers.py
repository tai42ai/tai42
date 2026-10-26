"""Env readers as plain data, and the env-name index built from them.

A description of a pydantic-settings class carries every env name it reads and how it nests, so
a process can judge which fields a name sets for a reader it never imported.
"""

from collections.abc import Iterator

import pytest
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from tai42_kit.settings import (
    EnvNameIndex,
    EnvReader,
    SettingsFieldRef,
    TaiBaseSettings,
    declare_env_reader,
    decode_env_readers,
    describe_env_reader,
    encode_env_readers,
    env_readers,
    on_env_reader_described,
    registered_settings,
    settings_fields_set_by,
)
from tai42_kit.settings import reader_descriptions as env_readers_mod
from tai42_kit.settings.registry import _REGISTRY, _clear_registry


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Run every test on an empty registry and no declared reader, then restore both."""
    saved = dict(_REGISTRY)
    declared = dict(env_readers_mod._DECLARED)
    listeners = list(env_readers_mod._DESCRIBED_LISTENERS)
    _clear_registry()
    env_readers_mod._DECLARED.clear()
    yield
    _clear_registry()
    _REGISTRY.update(saved)
    env_readers_mod._DECLARED.clear()
    env_readers_mod._DECLARED.update(declared)
    env_readers_mod._DESCRIBED_LISTENERS[:] = listeners


def _qualname(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _groups() -> tuple[type[TaiBaseSettings], type[TaiBaseSettings]]:
    class ReadersConnSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="READERS_CONN_")

        url: str = ""
        timeout: int = Field(default=5, validation_alias=AliasChoices("READERS_CONN_TIMEOUT", "READERS_WAIT"))

    class ReadersSettings(TaiBaseSettings):
        model_config = SettingsConfigDict(env_prefix="READERS_", env_nested_delimiter="__")

        conn: ReadersConnSettings = Field(default_factory=ReadersConnSettings)
        families: dict[str, dict[str, int]] = Field(default_factory=dict)
        level: str = "INFO"

    return ReadersConnSettings, ReadersSettings


_UNIVERSE = [
    "READERS_CONN_URL",
    "readers_conn_url",
    "READERS_CONN_TIMEOUT",
    "READERS_WAIT",
    "READERS_CONN",
    "readers_conn",
    "READERS_CONN__URL",
    "readers_conn__readers_wait",
    "READERS_CONN__TIMEOUT",
    "READERS_CONN__NOPE",
    "READERS_FAMILIES",
    "READERS_FAMILIES__EVENTS__LIMIT",
    "READERS_LEVEL",
    "READERS_LEVEL__X",
    "READERS_OTHER",
]


def test_the_index_of_the_described_groups_answers_as_the_registered_index_does():
    conn, parent = _groups()
    readers = decode_env_readers(encode_env_readers([describe_env_reader(parent), describe_env_reader(conn)]))
    index = EnvNameIndex.of(readers)

    assert {name: index.fields_set_by(name) for name in _UNIVERSE} == {
        name: settings_fields_set_by(name) for name in _UNIVERSE
    }
    assert index.fields_set_by("READERS_CONN") == {
        SettingsFieldRef(_qualname(conn), "url"),
        SettingsFieldRef(_qualname(conn), "timeout"),
    }


def test_the_encoding_of_a_reader_set_does_not_depend_on_its_order():
    conn, parent = _groups()
    readers = [describe_env_reader(parent), describe_env_reader(conn)]

    assert encode_env_readers(readers) == encode_env_readers(list(reversed(readers)))


def test_an_index_over_two_reader_sets_answers_for_the_names_of_both():
    conn, parent = _groups()

    class ReadersOtherSettings(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="READERS_OTHER_")
        level: str = "info"

    ours = [describe_env_reader(parent), describe_env_reader(conn)]
    index = EnvNameIndex.of([*ours, describe_env_reader(ReadersOtherSettings)])

    assert index.fields_set_by("READERS_CONN__URL") == {SettingsFieldRef(_qualname(conn), "url")}
    assert index.fields_set_by("readers_other_level") == {SettingsFieldRef(_qualname(ReadersOtherSettings), "level")}


def _version(*, with_extra: bool) -> EnvReader:
    """One description of the reader ``Versioned``, as two builds of it describe it."""
    if with_extra:

        class VersionedWithExtra(BaseSettings):
            model_config = SettingsConfigDict(env_prefix="READERS_V_", env_nested_delimiter="__")
            level: str = "info"
            extra: dict[str, str] = Field(default_factory=dict)

        cls: type[BaseSettings] = VersionedWithExtra
    else:

        class VersionedWithAlias(BaseSettings):
            model_config = SettingsConfigDict(env_prefix="READERS_V_")
            level: str = Field(default="info", validation_alias=AliasChoices("READERS_V_LEVEL", "READERS_V_LOUDNESS"))

        cls = VersionedWithAlias
    cls.__qualname__ = "Versioned"
    return describe_env_reader(cls)


def test_two_descriptions_of_one_reader_set_the_union_of_what_each_sets():
    old, new = _version(with_extra=False), _version(with_extra=True)
    assert old.qualname == new.qualname
    index = EnvNameIndex.of([old, new])

    assert index.fields_set_by("READERS_V_LOUDNESS") == {SettingsFieldRef(old.qualname, "level")}
    assert index.fields_set_by("READERS_V_EXTRA__KEY") == {SettingsFieldRef(old.qualname, "extra")}
    assert index.fields_set_by("READERS_V_LEVEL") == {SettingsFieldRef(old.qualname, "level")}


class _Sub(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="READERS_XS_")
    url: str | None = None
    concurrency: int = 10


class _Top(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="READERS_X_", env_nested_delimiter="__")
    sub: _Sub = Field(default_factory=_Sub)
    level: str = "info"


def test_a_declared_reader_s_nested_reader_is_reached_under_every_name_that_sets_its_fields():
    declare_env_reader(_Top)
    index = EnvNameIndex.of(env_readers())
    url = SettingsFieldRef(_qualname(_Sub), "url")

    assert index.fields_set_by("READERS_X_SUB__URL") == {url}
    assert index.fields_set_by("readers_xs_url") == {url}
    assert index.fields_set_by("READERS_X_SUB") == {url, SettingsFieldRef(_qualname(_Sub), "concurrency")}


def test_a_declared_reader_enters_no_registry():
    declare_env_reader(_Top)

    assert registered_settings() == []
    assert settings_fields_set_by("READERS_X_SUB__URL") == frozenset()
    assert {reader.qualname for reader in env_readers()} == {_qualname(_Top), _qualname(_Sub)}


def test_a_registration_and_a_declaration_each_notify_the_reader_listeners():
    described: list[str] = []
    on_env_reader_described(lambda reader: described.append(reader.qualname))

    conn, _parent = _groups()
    declare_env_reader(_Top)

    assert described[0] == _qualname(conn)
    assert described[-2:] == [_qualname(_Top), _qualname(_Sub)]


def test_a_registered_group_is_described_in_the_reader_set():
    conn, parent = _groups()

    assert sorted(reader.model_dump_json() for reader in env_readers()) == sorted(
        reader.model_dump_json() for reader in (describe_env_reader(conn), describe_env_reader(parent))
    )
