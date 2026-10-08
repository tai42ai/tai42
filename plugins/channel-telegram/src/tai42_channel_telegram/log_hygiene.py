"""Keep the Telegram bot token out of process logs.

The Bot API carries no non-URL auth: the token rides the request path
(``/bot<numeric id>:<secret>/sendMessage`` etc.), and ``httpx`` logs the full
request line at INFO (``HTTP Request: POST <url> ...``), which would spill the
token to any INFO sink. The token rule is registered with the kit's transport
redaction, which applies it to the ``httpx``/``httpcore`` loggers — the only
loggers that render the outbound URL — before any handler formats the record.

The rule is pattern-based, not value-based, so a rotated token needs no
re-registration and an unconfigured token still cannot leak. Only the token
segment is masked, so request observability survives.
"""

from __future__ import annotations

import re

from tai42_kit.logging import register_transport_redaction

# The token as it appears in a Bot API URL: the ``/bot`` path prefix, the numeric
# bot id, ``:``, then the secret up to the next path separator. Anchored on
# ``/bot`` so an unrelated word ending in "bot" cannot match. Both the id and the
# secret are masked — neither half reaches a sink.
_BOT_TOKEN_RE = re.compile(r"/bot\d+:[^/\s\"']+")


def _redact(text: str) -> str:
    return _BOT_TOKEN_RE.sub("/bot<redacted>", text)


def register_telegram_log_redaction() -> None:
    """Register the bot-token mask with the kit's transport redaction (replaces by name; idempotent)."""
    register_transport_redaction("telegram-bot-token", markers=("/bot",), redact=_redact)
