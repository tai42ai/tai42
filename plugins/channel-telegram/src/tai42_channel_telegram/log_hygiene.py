"""Keep the Telegram bot token out of process logs.

The Bot API carries no non-URL auth: the token rides the request path
(``/bot<numeric id>:<secret>/sendMessage`` etc.), and ``httpx`` logs the full
request line at INFO (``HTTP Request: POST <url> ...``), which would spill the
token to any INFO sink. This installs a redaction filter on the ``httpx`` and
``httpcore`` loggers — the only loggers that render the outbound URL — that masks
the ``/bot<id>:<secret>`` segment in every record before a handler formats it.

The filter runs in ``Logger.handle`` (before the record reaches any handler, own
or propagated), so the mask applies regardless of handler timing or logger
propagation. It is pattern-based, not value-based, so a rotated token needs no
re-install and an unconfigured token still cannot leak. Redaction preserves the
rest of the log line, so request observability survives the mask.
"""

from __future__ import annotations

import logging
import re

_REDACTION = "<redacted>"

# The token as it appears in a Bot API URL: the ``/bot`` path prefix, the numeric
# bot id, ``:``, then the secret up to the next path separator. Anchored on
# ``/bot`` so an unrelated word ending in "bot" cannot match. Both the id and the
# secret are masked — neither half reaches a sink.
_BOT_TOKEN_RE = re.compile(r"/bot\d+:[^/\s\"']+")

# Fail-closed replacement when rendering/redacting a record itself raises: the
# renderable text is dropped so a token it failed to mask can never reach a sink.
_REDACTOR_FAILED = "[telegram-log-redactor error: record suppressed]"

# The loggers that render the outbound request URL — the only leak path for the
# token, which never appears in this plugin's own log messages.
_REDACTED_LOGGERS = ("httpx", "httpcore")


def _redact(text: str) -> str:
    return _BOT_TOKEN_RE.sub(f"/bot{_REDACTION}", text)


class _TelegramTokenRedactor(logging.Filter):
    """Masks the Telegram bot token in a log record's rendered message.

    A record with no token marker passes untouched (the cheap pre-check pays only
    a substring/regex scan); a matching record is rendered once, masked, and its
    ``args`` cleared so a downstream formatter cannot re-interpolate the raw URL.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # The detection probe renders ``msg``/``args`` too, so it stays inside
            # the fail-closed guard: a raising ``__repr__`` (or ``__str__``) during
            # the pre-check must also fail closed, not escape ``filter``.
            raw_msg = record.msg if isinstance(record.msg, str) else str(record.msg)
            probe = raw_msg if not record.args else f"{raw_msg} {record.args!r}"
            if not _BOT_TOKEN_RE.search(probe):
                return True
            record.msg = _redact(record.getMessage())
            record.args = None
        except Exception:
            # A filter raises straight into the caller's ``log`` call (no
            # ``handleError`` guard at logger level), so a rendering fault — a bad
            # ``%`` arg count, a raising ``__repr__`` — must neither crash the send
            # path nor leak a token it failed to mask. Fail closed.
            record.msg = _REDACTOR_FAILED
            record.args = None
        return True


def install_telegram_log_redaction() -> None:
    """Attach the token-redaction filter to the ``httpx``/``httpcore`` loggers.

    Idempotent: a logger that already carries the redactor is left as is, so a
    plugin re-import (live reload) never stacks duplicate filters.
    """
    for name in _REDACTED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, _TelegramTokenRedactor) for f in logger.filters):
            logger.addFilter(_TelegramTokenRedactor())
