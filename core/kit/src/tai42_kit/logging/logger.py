import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_kit.logging.settings import LoggingSettings

#: The access logger whose request line carries the raw request path + query string.
_ACCESS_LOGGER_NAME = "uvicorn.access"
#: The fixed replacement every query-string value is collapsed to.
_QUERY_VALUE_MASK = "<redacted>"


def _mask_query_values(path_with_query: str) -> str:
    """Return ``path_with_query`` with every query-string VALUE replaced by a fixed mask,
    keys and structure intact. A request URL carries capability codes and opaque params by
    design, so no value may reach a log line; a query key is structural and stays."""
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
    filter redacts, it never drops."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            record.args = (*args[:2], _mask_query_values(args[2]), *args[3:])
        return True


def setup_logging(settings: "LoggingSettings") -> None:
    """Configure the ROOT logger for an application.

    Call this from application startup only — never on library import, since it
    reconfigures the process-global root logger. ``force=True`` replaces the
    existing root handlers, so a repeat call (e.g. after a settings reload)
    applies the new level instead of being silently ignored.

    The ``uvicorn.access`` logger's request line carries the full request URL — including
    query-string capability codes and opaque params — so it is fitted with a masking filter
    that redacts every query VALUE before the line is formatted. The attach is idempotent:
    a repeat call never stacks a second filter.
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
