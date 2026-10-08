"""A one-argument settings accessor cached per key.

The cache serves a value until ``reset_all_settings()`` drops it or the env file
changes identity; a value that is a settings instance is epoch-stamped so the
stale sweep reports a holder of a retired one; a raising build stores nothing.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic_settings import SettingsConfigDict

from tai42_kit.clients import advance_client_epoch
from tai42_kit.settings import (
    TaiBaseSettings,
    keyed_settings_cache,
    reset_all_settings,
    sweep_stale_settings,
)
from tai42_kit.settings.cache_registry import _CACHE_CLEARS


class _PrefixedSettings(TaiBaseSettings):
    registry_exclude: ClassVar[bool] = True
    model_config = SettingsConfigDict(env_prefix="KEYED_")

    value: str = "unset"


def _counting_accessor() -> tuple[list[str], Callable[[str], _PrefixedSettings]]:
    calls: list[str] = []

    @keyed_settings_cache
    def accessor(key: str) -> _PrefixedSettings:
        calls.append(key)
        return _PrefixedSettings(value=key)

    return calls, accessor


def test_a_second_call_for_a_key_builds_nothing() -> None:
    calls, accessor = _counting_accessor()

    first = accessor("one")
    assert accessor("one") is first
    assert accessor("two") is not first
    assert accessor("two").value == "two"
    assert calls == ["one", "two"]


def test_reset_all_settings_drops_every_entry() -> None:
    calls, accessor = _counting_accessor()
    first = accessor("one")

    reset_all_settings()

    assert accessor("one") is not first
    assert calls == ["one", "one"]


def test_the_clear_is_registered_under_the_accessor_name() -> None:
    @keyed_settings_cache
    def named_accessor(key: str) -> str:
        return key

    assert f"{__name__}.test_the_clear_is_registered_under_the_accessor_name.<locals>.named_accessor" in _CACHE_CLEARS
    assert named_accessor.__name__ == "named_accessor"


def test_a_changed_env_file_identity_rebuilds(tmp_path: Path) -> None:
    calls, accessor = _counting_accessor()
    (tmp_path / ".env").write_text("KEYED_OTHER=1\n")
    first = accessor("one")
    assert accessor("one") is first

    staged = tmp_path / ".env.staged"
    staged.write_text("KEYED_OTHER=2\n")
    os.replace(staged, tmp_path / ".env")

    rebuilt = accessor("one")
    assert rebuilt is not first
    assert accessor("one") is rebuilt
    assert calls == ["one", "one"]


def test_a_removed_env_file_rebuilds(tmp_path: Path) -> None:
    calls, accessor = _counting_accessor()
    (tmp_path / ".env").write_text("KEYED_OTHER=1\n")
    accessor("one")

    (tmp_path / ".env").unlink()
    accessor("one")

    assert calls == ["one", "one"]


def test_a_raising_build_stores_nothing() -> None:
    attempts: list[str] = []

    @keyed_settings_cache
    def flaky(key: str) -> str:
        attempts.append(key)
        if len(attempts) == 1:
            raise RuntimeError("build failed")
        return key.upper()

    with pytest.raises(RuntimeError, match="build failed"):
        flaky("one")
    assert flaky("one") == "ONE"
    assert flaky("one") == "ONE"
    assert attempts == ["one", "one"]


def test_a_held_settings_value_of_a_retired_epoch_is_reported_by_the_sweep() -> None:
    _, accessor = _counting_accessor()
    held = accessor("one")

    retired = advance_client_epoch()
    stale = sweep_stale_settings(retired)

    assert held is not None
    assert any(item.settings_type.endswith("_PrefixedSettings") for item in stale)


def test_a_primitive_value_is_cached_unstamped() -> None:
    @keyed_settings_cache
    def primitive(key: str) -> str | None:
        return None if key == "none" else key

    assert primitive("none") is None
    assert primitive("text") == "text"
