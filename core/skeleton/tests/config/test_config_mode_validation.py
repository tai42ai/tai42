"""ConfigModeSettings validation: the mode is used verbatim to resolve the provider
module, so a non-string or an out-of-shape value is rejected loudly and a valid
value is never normalized. An open (non-``file``) mode is accepted here and resolved
to a plugin by the factory, not restricted to a fixed set."""

from __future__ import annotations

import pytest

from tai42_skeleton.config.config_mode import ConfigMode, ConfigModeSettings, config_mode


def test_non_string_mode_raises() -> None:
    with pytest.raises(ValueError, match="Invalid TAI_CONFIG_MODE"):
        ConfigModeSettings(config_mode=123)  # type: ignore[arg-type]


def test_open_mode_is_accepted_verbatim() -> None:
    # Any [a-z][a-z0-9_]* name is a valid mode; the factory resolves it to the
    # tai42-config-<mode> plugin, so validation does not restrict it to a fixed set.
    settings = ConfigModeSettings(config_mode="external")
    assert settings.config_mode == "external"


def test_out_of_shape_mode_raises_not_normalized() -> None:
    # Uppercase / surrounding whitespace is rejected loudly rather than silently
    # lowered or stripped — the value must already be a usable module segment.
    for bad in ("  external ", "Vault", "has-hyphen", ""):
        with pytest.raises(ValueError, match="Invalid TAI_CONFIG_MODE"):
            ConfigModeSettings(config_mode=bad)  # pyright: ignore[reportArgumentType]


def test_config_mode_accessor_returns_the_mode_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_CONFIG_MODE", "external")
    config_mode.cache_clear()
    try:
        assert config_mode() == "external"
    finally:
        config_mode.cache_clear()


def test_config_mode_default_is_the_builtin_file_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_CONFIG_MODE", raising=False)
    config_mode.cache_clear()
    try:
        assert config_mode() == ConfigMode.file == "file"
    finally:
        config_mode.cache_clear()
