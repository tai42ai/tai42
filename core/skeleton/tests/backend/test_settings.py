"""BackendSettings defaults + the cached accessor."""

from __future__ import annotations

from tai42_skeleton.backend.settings import BackendSettings, base_backend_settings


def test_backend_settings_defaults() -> None:
    settings = BackendSettings()
    assert settings.manifest_key == "MANIFEST_KEY"
    assert settings.task_timeout == 300
    assert settings.tool_name_arg == "backend_tool_name"


def test_base_backend_settings_is_cached() -> None:
    base_backend_settings.cache_clear()
    try:
        first = base_backend_settings()
        assert isinstance(first, BackendSettings)
        # The accessor is memoized: same instance on the next call.
        assert base_backend_settings() is first
    finally:
        base_backend_settings.cache_clear()


def test_the_dispatch_fields_are_inherited_from_the_shared_dispatch_group() -> None:
    """The host's dispatch fields are INHERITED from the group every backend plugin shares,
    so the two sides of tool dispatch meet on the same names, defaults and reload classes by
    construction — the env surface under ``BACKEND_`` is unchanged."""
    from tai42_kit.backend import BackendDispatchSettings

    assert issubclass(BackendSettings, BackendDispatchSettings)
    assert BackendSettings.model_config.get("env_prefix") == "BACKEND_"
    assert set(BackendSettings.model_fields) == {"manifest_key", "task_timeout", "tool_name_arg"}
    assert "manifest_key" not in vars(BackendSettings).get("__annotations__", {})
