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


def test_the_dispatch_fields_match_the_shared_backend_mixin() -> None:
    """``BackendSettings`` and ``tai42_kit.backend.BackendDispatchSettings`` declare
    the SAME three fields for the two sides of tool dispatch — the host here, every
    backend plugin through the mixin — and they must agree on names, defaults and
    reload classes or the two sides stop meeting.

    They are two declaration sites rather than one because the API-diff gate loads
    this package with only itself on griffe's search path: an inherited member is
    invisible there and reads as a removed public attribute. This guard is what the
    inheritance would otherwise have given for free.
    """
    from tai42_kit.backend import BackendDispatchSettings

    def _declared(cls: type) -> dict[str, tuple[object, object]]:
        return {
            name: (field.default, (field.json_schema_extra or {}).get("reload"))  # pyright: ignore[reportAttributeAccessIssue]
            for name, field in cls.model_fields.items()  # pyright: ignore[reportAttributeAccessIssue]
        }

    assert _declared(BackendSettings) == _declared(BackendDispatchSettings)
    assert set(BackendSettings.model_fields) == {"manifest_key", "task_timeout", "tool_name_arg"}
