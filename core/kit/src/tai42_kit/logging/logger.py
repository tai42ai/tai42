"""Root-logger setup and filters that keep request URLs out of log lines."""

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from tai42_kit.logging.settings import LoggingSettings

#: The access logger whose request line carries the raw request path + query string.
_ACCESS_LOGGER_NAME = "uvicorn.access"
#: The fixed replacement every query-string value is collapsed to.
_QUERY_VALUE_MASK = "<redacted>"

#: The logger httpx logs every completed request line on, ``request.url`` included.
_HTTPX_LOGGER_NAME = "httpx"
#: The prefix of httpx's request-line log message (``'HTTP Request: %s %s "%s %d %s"'``),
#: whose second ``%s`` arg is the full ``request.url``.
_HTTPX_REQUEST_LINE_PREFIX = "HTTP Request:"


def _mask_query_values(path_with_query: str) -> str:
    """Return ``path_with_query`` with every query-string VALUE replaced by a fixed mask, keys and structure intact.

    A request URL carries capability codes and opaque params by design, so no value may reach a log line;
    a query key is structural and stays.
    """
    path, sep, query = path_with_query.partition("?")
    if not sep:
        return path_with_query
    masked = []
    for pair in query.split("&"):
        key, eq, _value = pair.partition("=")
        masked.append(f"{key}{eq}{_QUERY_VALUE_MASK}" if eq else pair)
    return f"{path}{sep}{'&'.join(masked)}"


class AccessLogQueryMaskingFilter(logging.Filter):
    """Mask every query-string value in the ``uvicorn.access`` request line.

    The access formatter REBUILDS the line from ``record.args`` — a 5-tuple whose third
    element is the request path + query — and ignores a rewritten ``record.msg``, so the
    mask is applied by replacing that args element; masking the message alone silently
    no-ops. Non-matching records pass through untouched. Always returns ``True`` — this
    filter redacts, it never drops.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Mask the request path in a matching access record and always keep the record."""
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (*args[:2], _mask_query_values(args[2]), *args[3:])
        return True


def _request_origin(url_value: object) -> str:
    """Return ``scheme://host[:port]`` for a request URL value, dropping path/query/userinfo.

    A URL-path or query credential (a bot token, a capability code) must never reach a
    log line, so only the origin — the part a log needs for observability — is kept.
    """
    parts = urlsplit(str(url_value))
    scheme = f"{parts.scheme}://" if parts.scheme else ""
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port is not None else ""
    return f"{scheme}{host}{port}"


class _HttpxRequestLineRedactor(logging.Filter):
    """Strip path and query from ``request.url`` in httpx's request-line log record.

    httpx logs every completed request as ``'HTTP Request: %s %s "%s %d %s"'`` with
    ``record.args = (method, request.url, http_version, status, reason)`` — the full URL,
    credential and all, at INFO. The line is REBUILT from ``record.args`` by the formatter,
    so the redaction replaces the URL arg with its origin only (``scheme://host[:port]``);
    rewriting ``record.msg`` alone would silently no-op. Non-matching records pass through
    untouched. Always returns ``True`` — this filter redacts, it never drops.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the URL arg in a matching httpx request-line record and always keep it."""
        args = record.args
        if (
            isinstance(record.msg, str)
            and record.msg.startswith(_HTTPX_REQUEST_LINE_PREFIX)
            and isinstance(args, tuple)
            and len(args) == 5
        ):
            record.args = (args[0], _request_origin(args[1]), *args[2:])
        return True


def setup_logging(settings: "LoggingSettings") -> None:
    """Configure the ROOT logger for an application.

    Call this from application startup only — never on library import, since it
    reconfigures the process-global root logger. ``force=True`` replaces the
    existing root handlers, so a repeat call (e.g. after a settings reload)
    applies the new level instead of being silently ignored.

    The ``uvicorn.access`` logger's request line carries the full request URL — including
    query-string capability codes and opaque params — so it is fitted with a masking filter
    that redacts every query VALUE before the line is formatted. The ``httpx`` logger's own
    request-line log carries the full outbound URL, credentials included, so it is fitted
    with a filter that strips the path and query, leaving only the origin. Both attaches are
    idempotent: a repeat call never stacks a second filter.
    """
    mapping = logging.getLevelNamesMapping()
    level_name = settings.log_level.upper()
    if level_name not in mapping:
        raise ValueError(f"Invalid log level: {settings.log_level!r}. Must be one of {sorted(mapping)}")
    logging.basicConfig(
        level=mapping[level_name],
        format="[%(asctime)s] %(levelname)-8s %(name)s %(message)-30s",
        datefmt="%m/%d/%y %H:%M:%S",
        force=True,
    )
    access_logger = logging.getLogger(_ACCESS_LOGGER_NAME)
    if not any(isinstance(existing, AccessLogQueryMaskingFilter) for existing in access_logger.filters):
        access_logger.addFilter(AccessLogQueryMaskingFilter())
    httpx_logger = logging.getLogger(_HTTPX_LOGGER_NAME)
    if not any(isinstance(existing, _HttpxRequestLineRedactor) for existing in httpx_logger.filters):
        httpx_logger.addFilter(_HttpxRequestLineRedactor())
