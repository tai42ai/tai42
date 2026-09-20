import pytest

from tai42_kit.logging.settings import LoggingSettings


class TestLoggingSettingsLogLevel:
    def test_default_info(self):
        assert LoggingSettings().log_level == "INFO"

    def test_is_enabled_for_normalizes_case(self):
        # is_enabled_for() upper-cases internally, so it works regardless of stored casing.
        settings = LoggingSettings(log_level="warning")
        assert settings.is_enabled_for("ERROR") is True
        assert settings.is_enabled_for("DEBUG") is False

    def test_is_enabled_for_at_threshold_is_inclusive(self):
        settings = LoggingSettings(log_level="INFO")
        assert settings.is_enabled_for("INFO") is True

    def test_valid_level_is_uppercased(self):
        assert LoggingSettings(log_level="debug").log_level == "DEBUG"

    def test_invalid_level_raises(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LoggingSettings(log_level="VERBOSE")

    def test_reads_prefixed_env_var(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TAI_LOG_LEVEL", "debug")
        assert LoggingSettings().log_level == "DEBUG"

    def test_ignores_unprefixed_env_var(self, monkeypatch: pytest.MonkeyPatch):
        # The knob is ``TAI_LOG_LEVEL`` only — a bare ``LOG_LEVEL`` is not read.
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        assert LoggingSettings().log_level == "INFO"
