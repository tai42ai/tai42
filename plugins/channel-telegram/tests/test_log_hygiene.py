"""The Telegram bot token must never reach a log sink.

The token rides every Bot API URL and httpx logs the request line at INFO. The
redaction filter installed on the ``httpx``/``httpcore`` loggers masks the
``/bot<id>:<secret>`` segment before any handler formats the record. These tests
drive both a synthetic httpx-shaped record and a real ``notify`` send.
"""

from __future__ import annotations

import logging

import pytest
from tai42_contract.channels import ChannelNotification

from tai42_channel_telegram.channel import TelegramChannel
from tai42_channel_telegram.log_hygiene import _REDACTOR_FAILED, _TelegramTokenRedactor, install_telegram_log_redaction

_TOKEN = "123456:test-token"
_SECRET = "test-token"
_URL = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"


@pytest.fixture(autouse=True)
def _clean_redactor_filters():
    """Isolate each test from the process-global filter list: strip any redactor
    (installed here or by a sibling module's register import) before and after."""

    def strip() -> None:
        for name in ("httpx", "httpcore"):
            logger = logging.getLogger(name)
            for redactor in [f for f in logger.filters if isinstance(f, _TelegramTokenRedactor)]:
                logger.removeFilter(redactor)

    strip()
    yield
    strip()


def _log_httpx_request(url: str) -> None:
    """Emit the exact record httpx writes for an outbound request."""
    logging.getLogger("httpx").info('HTTP Request: %s %s "%s %d %s"', "POST", url, "HTTP/1.1", 200, "OK")


def test_redactor_masks_token_in_httpx_request_line(caplog: pytest.LogCaptureFixture):
    install_telegram_log_redaction()
    with caplog.at_level(logging.INFO, logger="httpx"):
        _log_httpx_request(_URL)

    assert _TOKEN not in caplog.text
    assert _SECRET not in caplog.text
    assert "123456" not in caplog.text
    # The rest of the request line survives; only the token segment is masked.
    assert "/bot<redacted>/sendMessage" in caplog.text
    assert "HTTP Request: POST" in caplog.text


def test_non_telegram_httpx_record_passes_through_untouched(caplog: pytest.LogCaptureFixture):
    install_telegram_log_redaction()
    other = "https://example.test/api/thing?x=1"
    with caplog.at_level(logging.INFO, logger="httpx"):
        _log_httpx_request(other)

    assert other in caplog.text
    assert "<redacted>" not in caplog.text


def test_redaction_also_covers_httpcore_logger(caplog: pytest.LogCaptureFixture):
    install_telegram_log_redaction()
    with caplog.at_level(logging.INFO, logger="httpcore"):
        logging.getLogger("httpcore").info("connect_tcp.started url=%s", _URL)

    assert _TOKEN not in caplog.text
    assert _SECRET not in caplog.text
    assert "<redacted>" in caplog.text


def test_install_is_idempotent():
    install_telegram_log_redaction()
    install_telegram_log_redaction()
    httpx_filters = [f for f in logging.getLogger("httpx").filters if isinstance(f, _TelegramTokenRedactor)]
    httpcore_filters = [f for f in logging.getLogger("httpcore").filters if isinstance(f, _TelegramTokenRedactor)]
    assert len(httpx_filters) == 1
    assert len(httpcore_filters) == 1


def test_filter_fails_closed_on_raising_repr_during_detection():
    # The fail-closed guarantee must cover the DETECTION pre-check too: a record
    # whose args carry a raising ``__repr__`` renders during the probe, so any repr
    # fault there must fall into the fail-closed branch — never raise out of
    # ``filter()`` (which would crash the log call) nor leak an unmasked token.
    class _Boom:
        def __repr__(self) -> str:
            raise RuntimeError("repr exploded")

    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: %s",
        args=(_Boom(),),
        exc_info=None,
    )

    assert _TelegramTokenRedactor().filter(record) is True
    # Renderable text is blanked to the marker and the raw args dropped.
    assert record.msg == _REDACTOR_FAILED
    assert record.args is None


async def test_notify_send_does_not_log_the_token(http_recorder, caplog: pytest.LogCaptureFixture):
    # A real send through httpx: the request line is logged at INFO and must carry
    # no token. Asserting an httpx record was captured proves the mask actually
    # ran (not a vacuous pass from httpx staying silent).
    install_telegram_log_redaction()
    with caplog.at_level(logging.INFO, logger="httpx"):
        result = await TelegramChannel().notify(ChannelNotification(message="Deploy finished."))

    assert result == ["42"]
    assert str(http_recorder.requests[0].url) == _URL
    httpx_records = [r for r in caplog.records if r.name == "httpx"]
    assert httpx_records, "expected httpx to log the outbound request line"
    assert _TOKEN not in caplog.text
    assert _SECRET not in caplog.text
    assert "/bot<redacted>/sendMessage" in caplog.text
