"""``SandboxDispatchSettings`` — the sandbox lifecycle knobs, declared once.

The mixin carries the NAMES and DEFAULTS every provider group shares while each
concrete group keeps its own ``env_prefix``; the abstract mixin itself stays out
of the settings registry.
"""

from __future__ import annotations

import pytest
from pydantic_settings import SettingsConfigDict

from tai42_kit.sandbox import SandboxDispatchSettings
from tai42_kit.settings import registered_settings


class _DockerSandboxSettings(SandboxDispatchSettings):
    model_config = SettingsConfigDict(env_prefix="SANDBOX_DOCKER_")


class _LocalSandboxSettings(SandboxDispatchSettings):
    model_config = SettingsConfigDict(env_prefix="SANDBOX_LOCAL_")


def _fields(name: str) -> dict[str, object]:
    group = next(info for info in registered_settings() if info.name == name)
    return {field.name: field for field in group.fields}


def test_defaults_are_declared_once_for_every_provider() -> None:
    for settings in (_DockerSandboxSettings(), _LocalSandboxSettings()):
        assert settings.default_ttl_seconds == 3600
        assert settings.reap_interval_seconds == 300
        assert settings.exec_default_timeout_seconds == 300


def test_each_provider_keeps_its_own_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_DOCKER_DEFAULT_TTL_SECONDS", "60")
    monkeypatch.setenv("SANDBOX_LOCAL_DEFAULT_TTL_SECONDS", "120")

    assert _DockerSandboxSettings().default_ttl_seconds == 60
    assert _LocalSandboxSettings().default_ttl_seconds == 120


def test_the_mixin_itself_never_registers() -> None:
    names = {info.name for info in registered_settings()}
    assert "SandboxDispatchSettings" not in names
    assert {"_DockerSandboxSettings", "_LocalSandboxSettings"} <= names


def test_each_provider_registers_the_full_field_set() -> None:
    fields = _fields("_DockerSandboxSettings")
    assert set(fields) == {"default_ttl_seconds", "reap_interval_seconds", "exec_default_timeout_seconds"}
