"""The env file is parsed once per file identity and served to every settings class.

A settings construction reads the env file named by ``tai_env_file`` through the
kit's dotenv source, which serves a parse cached on the file's identity (device,
inode, mtime, size) and on the values of the variables the file interpolates. The
values every class resolves are those of a fresh parse; only the work is done once.
"""

import importlib
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import dotenv.main
import pytest
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import PydanticBaseSettingsSource, SettingsConfigDict

from tai42_kit.clients.settings import RedisConnectionSettings
from tai42_kit.settings import (
    DEFAULT_ENV_FILE,
    DefaultNamespaceMixin,
    TaiBaseSettings,
    env_file_identity,
    registered_settings,
)
from tai42_kit.settings import default_namespace as default_namespace_module
from tai42_kit.settings import env_file as env_file_module


@pytest.fixture(autouse=True)
def _empty_env_file_cache() -> Iterator[None]:
    env_file_module._ENV_FILE_CACHE.clear()
    yield
    env_file_module._ENV_FILE_CACHE.clear()


@pytest.fixture
def parse_count(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count every python-dotenv parse of a file or stream."""
    count = [0]
    original = dotenv.main.DotEnv.parse

    def _counting(self):
        count[0] += 1
        return original(self)

    monkeypatch.setattr(dotenv.main.DotEnv, "parse", _counting)
    return count


class SampleSettings(TaiBaseSettings):
    """A neutral group with a string, an int and a secret field."""

    registry_exclude: ClassVar[bool] = True
    model_config = SettingsConfigDict(env_prefix="SAMPLE_")

    name: str = "unset"
    size: int = 0
    token: SecretStr | None = None


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_default_env_file_is_the_cwd_dotenv() -> None:
    assert DEFAULT_ENV_FILE == ".env"
    assert TaiBaseSettings.tai_env_file == DEFAULT_ENV_FILE
    assert TaiBaseSettings.model_config.get("env_file") is None
    assert TaiBaseSettings.model_config.get("dotenv_filtering") == "only_existing"


def test_n_constructions_parse_the_file_once(tmp_path: Path, parse_count: list[int]) -> None:
    _write(tmp_path / ".env", "SAMPLE_NAME=alpha\nSAMPLE_SIZE=3\nSAMPLE_TOKEN=s3cret\n")

    for _ in range(25):
        settings = SampleSettings()
        assert settings.name == "alpha"
        assert settings.size == 3
        assert settings.token is not None
        assert settings.token.get_secret_value() == "s3cret"

    assert parse_count[0] == 1


def test_replaced_file_is_parsed_again(tmp_path: Path, parse_count: list[int]) -> None:
    _write(tmp_path / ".env", "SAMPLE_NAME=alpha\n")
    assert SampleSettings().name == "alpha"

    staged = _write(tmp_path / ".env.staged", "SAMPLE_NAME=beta\n")
    os.replace(staged, tmp_path / ".env")

    assert SampleSettings().name == "beta"
    assert SampleSettings().name == "beta"
    assert parse_count[0] == 2


def test_in_place_append_is_parsed_again(tmp_path: Path, parse_count: list[int]) -> None:
    path = _write(tmp_path / ".env", "SAMPLE_NAME=alpha\n")
    assert SampleSettings().size == 0

    with path.open("a") as fh:
        fh.write("SAMPLE_SIZE=7\n")

    assert SampleSettings().size == 7
    assert parse_count[0] == 2


def test_absent_file_reads_nothing(parse_count: list[int]) -> None:
    settings = SampleSettings()

    assert settings.name == "unset"
    assert parse_count[0] == 0


def test_identity_of_a_missing_path_or_a_directory_is_none(tmp_path: Path) -> None:
    assert env_file_identity(tmp_path / "missing.env") is None
    assert env_file_identity(tmp_path) is None
    assert env_file_identity(tmp_path / "missing" / "nested.env") is None

    path = _write(tmp_path / ".env", "A=1\n")
    stat = path.stat()
    assert env_file_identity(path) == (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)


def test_a_fifo_is_read_on_every_construction(tmp_path: Path, parse_count: list[int]) -> None:
    fifo = tmp_path / ".env"
    os.mkfifo(fifo)

    def _construct_while_feeding(payload: str) -> str:
        def _feed() -> None:
            with fifo.open("w") as writer:
                writer.write(payload)

        feeder = threading.Thread(target=_feed, daemon=True)
        feeder.start()
        name = SampleSettings().name
        feeder.join(timeout=5)
        assert not feeder.is_alive()
        return name

    assert _construct_while_feeding("SAMPLE_NAME=first\n") == "first"
    assert _construct_while_feeding("SAMPLE_NAME=second\n") == "second"
    assert parse_count[0] == 2


def test_interpolated_value_follows_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parse_count: list[int]
) -> None:
    _write(tmp_path / ".env", 'SAMPLE_NAME="${SAMPLE_ROOT}/data"\n')
    monkeypatch.setenv("SAMPLE_ROOT", "/srv/one")
    assert SampleSettings().name == "/srv/one/data"
    assert SampleSettings().name == "/srv/one/data"

    monkeypatch.setenv("SAMPLE_ROOT", "/srv/two")
    assert SampleSettings().name == "/srv/two/data"

    # The file was parsed once; only the interpolation was resolved again.
    assert parse_count[0] == 1


def test_interpolated_value_follows_a_full_environment_swap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / ".env", 'SAMPLE_NAME="${SAMPLE_ROOT:-fallback}/data"\n')
    monkeypatch.setenv("SAMPLE_ROOT", "/srv/live")
    assert SampleSettings().name == "/srv/live/data"

    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update({"SAMPLE_ROOT": "/srv/proposed"})
    try:
        assert SampleSettings().name == "/srv/proposed/data"
        os.environ.clear()
        assert SampleSettings().name == "fallback/data"
    finally:
        os.environ.clear()
        os.environ.update(saved)

    assert SampleSettings().name == "/srv/live/data"


def test_a_failed_read_stores_nothing_and_the_next_read_parses_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parse_count: list[int]
) -> None:
    _write(tmp_path / ".env", "SAMPLE_NAME=alpha\n")
    original = env_file_module.DotEnv.parse
    failures = [1]

    def _fail_once(self):
        if failures[0]:
            failures[0] -= 1
            raise OSError("read failed")
        return original(self)

    monkeypatch.setattr(env_file_module.DotEnv, "parse", _fail_once)

    with pytest.raises(OSError, match="read failed"):
        SampleSettings()
    assert env_file_module._ENV_FILE_CACHE == {}

    assert SampleSettings().name == "alpha"
    assert len(env_file_module._ENV_FILE_CACHE) == 1


def test_undecodable_file_raises_on_every_read(tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(b"SAMPLE_NAME=\xff\xfe\n")

    for _ in range(2):
        with pytest.raises(UnicodeDecodeError):
            SampleSettings()
    assert env_file_module._ENV_FILE_CACHE == {}


def test_a_class_naming_another_env_file(tmp_path: Path) -> None:
    other = _write(tmp_path / "other.env", "SAMPLE_NAME=from-other\n")
    _write(tmp_path / ".env", "SAMPLE_NAME=from-default\n")

    class OtherFileSettings(SampleSettings):
        tai_env_file: ClassVar[str | Path | None] = other

    class NoFileSettings(SampleSettings):
        tai_env_file: ClassVar[str | Path | None] = None

    assert OtherFileSettings().name == "from-other"
    assert NoFileSettings().name == "unset"
    assert SampleSettings().name == "from-default"


def test_model_config_env_file_is_refused() -> None:
    with pytest.raises(TypeError, match="tai_env_file"):

        class DeclaresEnvFile(TaiBaseSettings):
            registry_exclude: ClassVar[bool] = True
            model_config = SettingsConfigDict(env_file=".custom.env")

            value: str = ""


class MixinStoreSettings(DefaultNamespaceMixin, TaiBaseSettings):
    """A neutral store whose identity falls back to the shared namespace."""

    registry_exclude: ClassVar[bool] = True
    model_config = SettingsConfigDict(env_prefix="MIXSTORE_")
    tai_default_fields: ClassVar[dict[str, str]] = {"host": "host"}

    host: str | None = None
    port: int = 1


def test_mixin_default_namespace_resolves_from_the_file_with_one_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parse_count: list[int]
) -> None:
    monkeypatch.delenv("TAI_DEFAULT_HOST", raising=False)
    _write(tmp_path / ".env", "TAI_DEFAULT_HOST=shared-host\nMIXSTORE_PORT=9\n")

    for _ in range(5):
        settings = MixinStoreSettings()
        assert settings.host == "shared-host"
        assert settings.port == 9

    # The specific dotenv source and the default-namespace dotenv source share one parse.
    assert parse_count[0] == 1


class _NullSource(PydanticBaseSettingsSource):
    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return {}


def test_mixin_sources_wrap_the_kit_dotenv_source() -> None:
    passed = {
        name: _NullSource(MixinStoreSettings)
        for name in ("init_settings", "env_settings", "dotenv_settings", "file_secret_settings")
    }
    sources = MixinStoreSettings.settings_customise_sources(MixinStoreSettings, **passed)
    inner = [getattr(source, "_inner", source) for source in sources]

    assert inner[0] is passed["init_settings"]
    assert inner[1] is passed["env_settings"]
    assert isinstance(inner[2], env_file_module.TaiDotEnvSettingsSource)
    assert inner[3] is passed["file_secret_settings"]
    assert all(source is not passed["dotenv_settings"] for source in inner)


class _Nested(BaseModel):
    host: str = "nested-default"
    port: int = 0


class NestedSettings(TaiBaseSettings):
    """A neutral group with a nested-delimiter model field."""

    registry_exclude: ClassVar[bool] = True
    model_config = SettingsConfigDict(env_prefix="NESTED_", env_nested_delimiter="__")

    inner: _Nested = Field(default_factory=_Nested)
    plain: str = "plain-default"


def test_only_existing_resolves_what_the_extras_loop_resolved(tmp_path: Path) -> None:
    _write(
        tmp_path / ".env",
        "NESTED_INNER__HOST=from-file\nNESTED_INNER__PORT=8\nNESTED_PLAIN=p\nNESTED_UNKNOWN=x\nUNRELATED=y\n",
    )

    class ExtrasLoopNestedSettings(NestedSettings):
        registry_exclude: ClassVar[bool] = True
        model_config = SettingsConfigDict(dotenv_filtering=None)

    assert ExtrasLoopNestedSettings.model_config.get("dotenv_filtering") is None
    assert NestedSettings().model_dump() == ExtrasLoopNestedSettings().model_dump()
    assert NestedSettings().inner.host == "from-file"


def _registered_classes() -> list[type[TaiBaseSettings]]:
    """Every settings group the registry holds, plus every imported group it registers.

    Another test may clear the registry after the groups' modules were imported; the
    subclass walk applies the registration rule (own ``registry_exclude`` unset, at
    least one field) so the set does not depend on test order.
    """
    found: dict[str, type[TaiBaseSettings]] = {}
    for info in registered_settings():
        if "<locals>" in info.qualname:
            continue
        obj: Any = importlib.import_module(info.module)
        for part in info.qualname[len(info.module) + 1 :].split("."):
            obj = getattr(obj, part)
        found[info.qualname] = obj
    pending: list[type[TaiBaseSettings]] = [TaiBaseSettings]
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if "<locals>" in cls.__qualname__ or cls.__module__.startswith("tests"):
            continue
        if cls.__dict__.get("registry_exclude") is True or not cls.model_fields:
            continue
        found.setdefault(f"{cls.__module__}.{cls.__qualname__}", cls)
    return list(found.values())


def _dump_or_error(cls: type[TaiBaseSettings]) -> Any:
    try:
        return cls().model_dump()
    except Exception as exc:
        return (type(exc).__name__, str(exc))


def _fixture_env(tmp_path: Path) -> None:
    lines = [f"UNRELATED_{i:03d}=value_{i:03d}" for i in range(60)]
    lines += [
        "MCP_CLIENT_CONNECT_TIMEOUT_SECONDS=12",
        "MCP_CLIENT_CALL_TIMEOUT_SECONDS=34",
        "TAI_DEFAULT_REDIS_URL=redis://cache.invalid:6379/0",
        "LLM_PROVIDER=openai",
        'TAI_LOG_LEVEL="${SAMPLE_LEVEL:-info}"',
    ]
    _write(tmp_path / ".env", "\n".join(lines) + "\n")


def test_every_registered_class_resolves_the_same_values_with_and_without_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tai42_kit.clients.settings
    import tai42_kit.llm.settings
    import tai42_kit.logging  # noqa: F401

    _fixture_env(tmp_path)

    # Every kit class that mixes in the default namespace is a registry-excluded base,
    # so a neutral consumer of the mixin joins the set to carry the TAI_DEFAULT_* fallback.
    class SampleStoreRedis(RedisConnectionSettings):
        registry_exclude: ClassVar[bool] = True
        model_config = SettingsConfigDict(env_prefix="SAMPLESTORE_")

    classes = [*_registered_classes(), SampleStoreRedis]

    cached = {cls: _dump_or_error(cls) for cls in classes}
    cached_again = {cls: _dump_or_error(cls) for cls in classes}

    def _uncached(path: Path, *, encoding: str | None) -> dict[str, str | None]:
        return dict(dotenv.main.dotenv_values(path, encoding=encoding or "utf8"))

    # Both sources look the reader up by name in their own module: the base dotenv
    # source in ``env_file``, the ``TAI_DEFAULT_*`` fallback in ``default_namespace``.
    monkeypatch.setattr(env_file_module, "read_env_file", _uncached)
    monkeypatch.setattr(default_namespace_module, "read_env_file", _uncached)
    fresh = {cls: _dump_or_error(cls) for cls in classes}

    assert fresh[SampleStoreRedis]["redis_url"] == "redis://cache.invalid:6379/0"
    assert cached == fresh
    assert cached_again == fresh


def test_every_registered_class_resolves_the_same_values_with_only_existing(tmp_path: Path) -> None:
    import tai42_kit.clients.settings
    import tai42_kit.llm.settings
    import tai42_kit.logging  # noqa: F401

    _fixture_env(tmp_path)
    for cls in _registered_classes():

        class ExtrasLoop(cls):  # type: ignore[valid-type,misc]
            registry_exclude: ClassVar[bool] = True
            model_config = SettingsConfigDict(dotenv_filtering=None)

        assert _dump_or_error(cls) == _dump_or_error(ExtrasLoop), cls.__qualname__


def test_redis_connection_settings_resolve_the_shared_url_from_the_file(tmp_path: Path) -> None:
    _write(tmp_path / ".env", "TAI_DEFAULT_REDIS_URL=redis://shared.invalid:6379/0\n")

    class StoreRedis(RedisConnectionSettings):
        registry_exclude: ClassVar[bool] = True
        model_config = SettingsConfigDict(env_prefix="SAMPLESTORE_")

    assert StoreRedis().redis_url == "redis://shared.invalid:6379/0"
