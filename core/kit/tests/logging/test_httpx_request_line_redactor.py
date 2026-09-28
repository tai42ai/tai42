"""The httpx request-line redactor: strip path/query from the outbound URL a log line carries.

httpx logs every completed request as ``'HTTP Request: %s %s "%s %d %s"'`` with
``record.args = (method, request.url, http_version, status, reason)`` — the full URL at
INFO. The redactor rewrites the URL arg to its origin only, so a URL-path or query
credential never reaches a sink; ``setup_logging`` fits it on the ``httpx`` logger.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from tai42_kit.logging.logger import (
    _HttpxRequestLineRedactor,
    _request_origin,
    setup_logging,
)
from tai42_kit.logging.settings import LoggingSettings

_HTTPX_REQUEST_LINE_FMT = 'HTTP Request: %s %s "%s %d %s"'


def _request_line_record(url: object) -> logging.LogRecord:
    """An httpx request-line record exactly as the library emits it (the URL is args[1])."""
    return logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=_HTTPX_REQUEST_LINE_FMT,
        args=("GET", url, "1.1", 200, "OK"),
        exc_info=None,
    )


def test_request_origin_drops_path_query_and_userinfo():
    assert _request_origin("https://user:pass@api.example.com:8443/file/bot1234:SECRET/x?y=z") == (
        "https://api.example.com:8443"
    )
    assert _request_origin("http://127.0.0.1:9000/a/b") == "http://127.0.0.1:9000"
    assert _request_origin("https://host/path") == "https://host"


def test_redactor_rewrites_url_arg_to_origin_only():
    record = _request_line_record("https://api.telegram.org/file/bot1234567:AAsecret/photo.jpg?dl=1")
    assert _HttpxRequestLineRedactor().filter(record) is True
    assert isinstance(record.args, tuple)
    assert record.args[1] == "https://api.telegram.org"
    line = record.getMessage()
    assert "bot1234567:AAsecret" not in line
    assert "/file/bot" not in line
    assert "dl=1" not in line
    assert "https://api.telegram.org" in line
    assert '"1.1 200 OK"' in line  # method + version + status still rendered


def test_redactor_handles_a_real_httpx_url_object():
    record = _request_line_record(httpx.URL("https://cdn.example.com:8443/media/SECRET-TOKEN/blob"))
    _HttpxRequestLineRedactor().filter(record)
    assert isinstance(record.args, tuple)
    assert record.args[1] == "https://cdn.example.com:8443"
    assert "SECRET-TOKEN" not in record.getMessage()


def test_redactor_leaves_a_non_request_line_record_untouched():
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="load_ssl_context verify=%r",
        args=(True,),
        exc_info=None,
    )
    _HttpxRequestLineRedactor().filter(record)
    assert record.args == (True,)  # not the request-line 5-tuple shape


def test_redactor_always_returns_true_and_keeps_the_record():
    record = _request_line_record("https://host/p")
    assert _HttpxRequestLineRedactor().filter(record) is True


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


@pytest.fixture
def httpx_logger_restored():
    """Snapshot the httpx logger's filters; setup_logging attaches the redactor to it."""
    httpx_logger = logging.getLogger("httpx")
    filters = httpx_logger.filters[:]
    try:
        yield httpx_logger
    finally:
        httpx_logger.filters[:] = filters


def test_setup_logging_attaches_the_redactor_once(root_logger_restored, httpx_logger_restored):
    setup_logging(LoggingSettings(log_level="INFO"))
    setup_logging(LoggingSettings(log_level="INFO"))
    redactors = [f for f in httpx_logger_restored.filters if isinstance(f, _HttpxRequestLineRedactor)]
    assert len(redactors) == 1  # idempotent: a re-setup never stacks a second filter
