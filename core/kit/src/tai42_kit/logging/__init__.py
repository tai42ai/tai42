"""Logging settings, setup and the process log-redaction registries.

Side-effect-free on import: the consumer calls ``setup_logging(...)`` and
``install_record_redaction(...)`` explicitly (e.g. at app startup), and a feature
registers its own redaction patterns — importing this package configures nothing.
"""

from tai42_kit.logging.logger import AccessLogQueryMaskingFilter, setup_logging
from tai42_kit.logging.redaction import (
    REDACTOR_FAILED,
    TRANSPORT_LOGGERS,
    URL_REDACTION,
    RecordScope,
    install_record_redaction,
    redact_url_userinfo,
    redact_urls_in_text,
    register_record_redactor,
    register_transport_redaction,
)
from tai42_kit.logging.settings import LoggingSettings, logging_settings

__all__ = [
    "REDACTOR_FAILED",
    "TRANSPORT_LOGGERS",
    "URL_REDACTION",
    "AccessLogQueryMaskingFilter",
    "LoggingSettings",
    "RecordScope",
    "install_record_redaction",
    "logging_settings",
    "redact_url_userinfo",
    "redact_urls_in_text",
    "register_record_redactor",
    "register_transport_redaction",
    "setup_logging",
]
