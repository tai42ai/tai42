"""The Telegram bot token must never reach a log sink.

The token rides every Bot API URL and httpx logs the request line at INFO. The
token rule registered with the kit's transport redaction masks the
``/bot<id>:<secret>`` segment on the ``httpx``/``httpcore`` loggers before any
handler formats the record. These tests drive both a synthetic httpx-shaped record
and a real ``notify`` send.
"""

from __future__ import annotations

import logging

import pytest
from tai42_contract.channels import ChannelNotification
from tai42_kit.logging import REDACTOR_FAILED, TRANSPORT_LOGGERS
from tai42_kit.logging import redaction as kit_redaction

from tai42_channel_telegram.channel import TelegramChannel
from tai42_channel_telegram.log_hygiene import register_telegram_log_redaction

_TOKEN = "123456:test-token"
_SECRET = "test-token"
_URL = f"https://api.telegram.org/bot{_TOKEN}/sendMessage"


@pytest.fixture(autouse=True)
def _clean_transport_redaction(monkeypatch: pytest.MonkeyPatch):
    """Isolate each test from the process-global transport redaction: an empty kit
    registry and no transport filter (one a sibling module's register import may have
    installed) before and after."""

    def strip() -> None:
        for name in TRANSPORT_LOGGERS:
            logger = logging.getLogger(name)
            for existing in [f for f in logger.filters if isinstance(f, kit_redaction._TransportRedactionFilter)]:
                logger.removeFilter(existing)

    monkeypatch.setattr(kit_redaction, "_TRANSPORT_REDACTORS", kit_redaction._Registry())
    strip()
    yield
    strip()


def _log_httpx_request(url: str) -> None:
    """Emit the exact record httpx writes for an outbound request."""
    logging.getLogger("httpx").info('HTTP Request: %s %s "%s %d %s"', "POST", url, "HTTP/1.1", 200, "OK")


def test_redactor_masks_token_in_httpx_request_line(caplog: pytest.LogCaptureFixture):
    register_telegram_log_redaction()
    with caplog.at_level(logging.INFO, logger="httpx"):
        _log_httpx_request(_URL)

    assert _TOKEN not in caplog.text
    assert _SECRET not in caplog.text
    assert "123456" not in caplog.text
    # The rest of the request line survives; only the token segment is masked.
    assert "/bot<redacted>/sendMessage" in caplog.text
    assert "HTTP Request: POST" in caplog.text


def test_non_telegram_httpx_record_passes_through_untouched(caplog: pytest.LogCaptureFixture):
    register_telegram_log_redaction()
    other = "https://example.test/api/thing?x=1"
    with caplog.at_level(logging.INFO, logger="httpx"):
        _log_httpx_request(other)

    assert other in caplog.text
    assert "<redacted>" not in caplog.text


def test_redaction_also_covers_httpcore_logger(caplog: pytest.LogCaptureFixture):
    register_telegram_log_redaction()
    with caplog.at_level(logging.INFO, logger="httpcore"):
        logging.getLogger("httpcore").info("connect_tcp.started url=%s", _URL)

    assert _TOKEN not in caplog.text
    assert _SECRET not in caplog.text
    assert "<redacted>" in caplog.text


def test_registering_twice_keeps_one_rule_and_one_filter_per_transport_logger():
    register_telegram_log_redaction()
    register_telegram_log_redaction()
    assert len(kit_redaction._TRANSPORT_REDACTORS.active) == 1
    for name in TRANSPORT_LOGGERS:
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, kit_redaction._TransportRedactionFilter)]
        assert len(filters) == 1


def test_a_raising_repr_on_a_transport_record_fails_closed(caplog: pytest.LogCaptureFixture):
    # A record whose args carry a raising ``__repr__`` renders during detection: the
    # record fails closed — the log call does not crash and nothing unmasked passes.
    class _Boom:
        def __repr__(self) -> str:
            raise RuntimeError("repr exploded")

        __str__ = __repr__

    register_telegram_log_redaction()
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info("HTTP Request: %s", _Boom())

    assert caplog.records[0].getMessage() == REDACTOR_FAILED
    assert caplog.records[0].args is None


async def test_notify_send_does_not_log_the_token(http_recorder, caplog: pytest.LogCaptureFixture):
    # A real send through httpx: the request line is logged at INFO and must carry
    # no token. Asserting an httpx record was captured proves the mask actually
    # ran (not a vacuous pass from httpx staying silent).
    register_telegram_log_redaction()
    with caplog.at_level(logging.INFO, logger="httpx"):
        result = await TelegramChannel().notify(ChannelNotification(message="Deploy finished."))

    assert result == ["42"]
    assert str(http_recorder.requests[0].url) == _URL
    httpx_records = [r for r in caplog.records if r.name == "httpx"]
    assert httpx_records, "expected httpx to log the outbound request line"
    assert _TOKEN not in caplog.text
    assert _SECRET not in caplog.text
    assert "/bot<redacted>/sendMessage" in caplog.text
