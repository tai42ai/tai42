"""TaiBaseSettings fallback helpers, setup_logging, and LoggingSettings level
checks."""

import logging
from typing import cast

import pytest

from tai42_kit.logging.logger import AccessLogQueryMaskingFilter, setup_logging
from tai42_kit.logging.settings import LoggingSettings
from tai42_kit.settings.base import TaiBaseSettings

# The access logger name + request format string uvicorn emits: args[2] is the path+query.
_ACCESS_LOGGER = "uvicorn.access"
_UVICORN_ACCESS_FMT = '%s - "%s %s HTTP/%s" %d'


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


# -- access-log query-value masking --------------------------------------------


def _access_record(path_with_query: str) -> logging.LogRecord:
    """A uvicorn.access record exactly as the library emits it: the request line is REBUILT
    from ``args`` (whose third element is the path + query), not from a pre-formatted msg."""
    return logging.LogRecord(
        name=_ACCESS_LOGGER,
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=_UVICORN_ACCESS_FMT,
        args=("127.0.0.1:54321", "GET", path_with_query, "1.1", 200),
        exc_info=None,
    )


def test_access_filter_masks_query_values_keeping_keys():
    record = _access_record("/api/chat?tai_entry=SECRETCODE&x=hunter2&flag")
    assert AccessLogQueryMaskingFilter().filter(record) is True
    assert isinstance(record.args, tuple)
    # Values masked, keys and structure intact — including a bare valueless key.
    assert record.args[2] == "/api/chat?tai_entry=<redacted>&x=<redacted>&flag"
    # The line the formatter actually emits (rebuilt from args) carries no value.
    line = record.getMessage()
    assert "SECRETCODE" not in line
    assert "hunter2" not in line
    assert "tai_entry=<redacted>" in line


def test_access_filter_leaves_a_path_only_line_untouched():
    record = _access_record("/api/health")
    AccessLogQueryMaskingFilter().filter(record)
    assert isinstance(record.args, tuple)
    assert record.args[2] == "/api/health"


def test_access_filter_masks_a_value_that_itself_contains_equals():
    record = _access_record("/p?token=a=b=c&y=1")
    AccessLogQueryMaskingFilter().filter(record)
    assert isinstance(record.args, tuple)
    # The split is on the FIRST '=' only, so the whole value (equals and all) is redacted.
    assert record.args[2] == "/p?token=<redacted>&y=<redacted>"


def test_access_filter_ignores_a_record_that_is_not_the_access_shape():
    record = logging.LogRecord(
        name="some.other.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="plain %s",
        args=("value",),
        exc_info=None,
    )
    AccessLogQueryMaskingFilter().filter(record)
    assert record.args == ("value",)  # untouched: not the 5-tuple access request line


@pytest.fixture
def access_logger_restored():
    """Snapshot the uvicorn.access logger's filters; setup_logging attaches to it."""
    access = logging.getLogger(_ACCESS_LOGGER)
    filters = access.filters[:]
    try:
        yield access
    finally:
        access.filters[:] = filters


def test_setup_logging_attaches_the_masking_filter_once(root_logger_restored, access_logger_restored):
    setup_logging(LoggingSettings(log_level="INFO"))
    setup_logging(LoggingSettings(log_level="INFO"))
    masks = [f for f in access_logger_restored.filters if isinstance(f, AccessLogQueryMaskingFilter)]
    assert len(masks) == 1  # idempotent: a re-setup never stacks a second filter
