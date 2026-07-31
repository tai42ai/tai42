"""TaiBaseSettings fallback helpers, setup_logging, and LoggingSettings level
checks."""

import logging
from typing import cast

import pytest

from tai42_kit.logging.logger import setup_logging
from tai42_kit.logging.settings import LoggingSettings
from tai42_kit.settings.base import TaiBaseSettings


class _Cfg(TaiBaseSettings):
    host: str = "localhost"
    port: int = 8080
    note: str | None = None


def test_with_fallbacks_user_wins_and_none_dropped():
    cfg = _Cfg()
    merged = cfg.with_fallbacks({"port": 9000})
    # User value wins on conflict; defaults fill the rest; None-valued 'note' is dropped.
    assert merged == {"host": "localhost", "port": 9000}


def test_with_fallbacks_empty_user_config():
    assert _Cfg().with_fallbacks({}) == {"host": "localhost", "port": 8080}
    # None exercises the `user_config or {}` guard that falls back to defaults.
    assert _Cfg().with_fallbacks(cast(dict, None)) == {"host": "localhost", "port": 8080}


def test_load_with_fallbacks_parses_json():
    merged = _Cfg().load_with_fallbacks('{"host": "remote"}')
    assert merged == {"host": "remote", "port": 8080}


def test_load_with_fallbacks_empty_string_uses_defaults():
    assert _Cfg().load_with_fallbacks("") == {"host": "localhost", "port": 8080}


@pytest.fixture
def root_logger_restored():
    """Snapshot the root logger; setup_logging (force=True) replaces its handlers."""
    root = logging.getLogger()
    level, handlers = root.level, root.handlers[:]
    try:
        yield root
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def test_setup_logging_sets_root_level(root_logger_restored):
    setup_logging(LoggingSettings(log_level="WARNING"))
    assert root_logger_restored.level == logging.WARNING


def test_setup_logging_reapplies_on_second_call(root_logger_restored):
    # force=True replaces the handlers basicConfig installed before, so a
    # re-setup (settings reload) changes the level instead of being a no-op.
    setup_logging(LoggingSettings(log_level="INFO"))
    setup_logging(LoggingSettings(log_level="WARNING"))
    assert root_logger_restored.level == logging.WARNING
    assert len(root_logger_restored.handlers) == 1  # replaced, not accumulated


def test_setup_logging_format_includes_logger_name(root_logger_restored):
    setup_logging(LoggingSettings(log_level="INFO"))
    formatter = root_logger_restored.handlers[0].formatter
    assert formatter is not None
    assert "%(name)s" in formatter._fmt  # type: ignore[union-attr]


def test_setup_logging_rejects_unknown_level():
    # model_construct skips pydantic validation, so setup_logging's own level
    # check is what raises here — naming the bad value. It raises before
    # touching the root logger, so nothing needs restoring.
    with pytest.raises(ValueError, match="LOUD"):
        setup_logging(LoggingSettings.model_construct(log_level="LOUD"))


def test_logging_settings_validates_and_uppercases():
    assert LoggingSettings(log_level="debug").log_level == "DEBUG"


def test_logging_settings_rejects_unknown_level():
    with pytest.raises(ValueError, match="Invalid log level"):
        LoggingSettings(log_level="LOUD")


def test_logging_settings_is_enabled_for():
    s = LoggingSettings(log_level="WARNING")
    assert s.is_enabled_for("ERROR") is True
    assert s.is_enabled_for("DEBUG") is False
    assert s._level_num() == logging.WARNING
