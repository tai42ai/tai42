"""``BackendDispatchSettings`` — the dispatch surface both sides declare once.

What the mixin has to preserve is the ENV SURFACE of each side that inherits it:
same names, same defaults, same reload classes, each under its own prefix. The
mixin itself must stay out of the settings registry, since its field names are
unprefixed and belong to no side.
"""

from __future__ import annotations

import pytest
from pydantic_settings import SettingsConfigDict

from tai42_kit.backend import BackendDispatchSettings
from tai42_kit.settings import registered_settings


class _HostSettings(BackendDispatchSettings):
    model_config = SettingsConfigDict(env_prefix="HOSTSIDE_")


class _AdapterSettings(BackendDispatchSettings):
    model_config = SettingsConfigDict(env_prefix="ADAPTERSIDE_")


def _fields(name: str) -> dict[str, object]:
    group = next(info for info in registered_settings() if info.name == name)
    return {field.name: field for field in group.fields}


def test_defaults_are_declared_once_for_every_side() -> None:
    host = _HostSettings()
    adapter = _AdapterSettings()

    for settings in (host, adapter):
        assert settings.manifest_key == "MANIFEST_KEY"
        assert settings.task_timeout == 300
        assert settings.tool_name_arg == "backend_tool_name"


def test_each_side_keeps_its_own_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``model_config`` merges down the MRO, so the subclass's prefix wins and the
    # env surface is exactly what it was before the mixin.
    monkeypatch.setenv("HOSTSIDE_TOOL_NAME_ARG", "host_arg")
    monkeypatch.setenv("ADAPTERSIDE_TOOL_NAME_ARG", "adapter_arg")

    assert _HostSettings().tool_name_arg == "host_arg"
    assert _AdapterSettings().tool_name_arg == "adapter_arg"


def test_boot_pinned_fields_are_recycle_class() -> None:
    # A forked child reads the env key it was given and a queued job carries the
    # kwarg name the producer used, so neither converges in-process.
    fields = _fields("_HostSettings")
    assert fields["manifest_key"].reload_class == "recycle"  # pyright: ignore[reportAttributeAccessIssue]
    assert fields["tool_name_arg"].reload_class == "recycle"  # pyright: ignore[reportAttributeAccessIssue]
    # The dispatch result timeout is read per call, so it flips live.
    assert fields["task_timeout"].reload_class == "hot"  # pyright: ignore[reportAttributeAccessIssue]


def test_the_mixin_itself_never_registers() -> None:
    # Its field names are unprefixed and belong to no side; only the concrete
    # per-side subclasses are real env groups.
    names = {info.name for info in registered_settings()}
    assert "BackendDispatchSettings" not in names
    assert {"_HostSettings", "_AdapterSettings"} <= names


def test_each_side_registers_the_full_field_set() -> None:
    fields = _fields("_AdapterSettings")
    assert set(fields) == {"manifest_key", "task_timeout", "tool_name_arg"}
    assert fields["manifest_key"].env_var == "ADAPTERSIDE_MANIFEST_KEY"  # pyright: ignore[reportAttributeAccessIssue]
