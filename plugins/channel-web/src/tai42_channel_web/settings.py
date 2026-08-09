"""Web chat channel settings.

A ``TaiBaseSettings`` subclass reading the ``CHANNEL_WEB_`` env group through
accessors cached by ``tai42_kit.settings.settings_cache`` (dropped on a soft
restart). This channel holds no vendor secret: its doors are public and
authenticate the visitor by the session cookie alone, and the only backing store
is a plugin-owned Redis holding the session registrations, the transcripts, and
the pending-question records (``CHANNEL_WEB_REDIS_URL`` etc., falling back
per-field to the shared ``TAI_DEFAULT_*`` namespace).
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.settings import TaiBaseSettings, settings_cache


class WebSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHANNEL_WEB_")

    # Timeout for the loopback answer forward to the interactions callback door.
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    # XADD ``MAXLEN`` cap on the browser-replay transcript stream, trimmed EXACTLY
    # (never the ``~`` approximation): the newest entries are kept, older ones
    # trimmed — the durable record of a turn lives in the conversation bridge, not
    # here.
    transcript_max_entries: int = Field(default=1000, gt=0)
    # Transcript entries read per XRANGE page when an SSE stream replays a backlog.
    # Peak memory for a replay is one page, not the whole transcript.
    backlog_batch_entries: int = Field(default=200, gt=0)
    # Seconds; EXPIRE refreshed on every append so an idle transcript ages out
    # rather than living forever (30 days).
    transcript_ttl_seconds: int = Field(default=30 * 86400, gt=0)
    # Seconds; grace ADDED to the keepalive block window for the outer
    # ``asyncio.wait_for`` that bounds a black-holed Redis XREAD — a stalled tail
    # raises loudly rather than hanging the SSE generator forever.
    blocking_grace_seconds: float = Field(default=5.0, gt=0)
    # Seconds between SSE keepalive comment frames; also the maximum a live-tail
    # XREAD blocks per iteration (the block window is the time to the next
    # keepalive deadline).
    keepalive_seconds: int = Field(default=15, ge=1)
    # ``Secure`` on the visitor session cookie, and with it the cookie's NAME and
    # ``Path``: Secure mints ``__Host-tai_web_session`` at ``/`` (the prefix a browser
    # honors only on a Secure, path-root, domain-less cookie), plain http the bare
    # ``tai_web_session`` under this plugin's own prefix — see ``session.py``. False
    # ONLY where the page is served over plain http (dev / e2e): a Secure cookie is
    # never stored there, and the visitor would get a fresh session on every request.
    session_cookie_secure: bool = True
    # Seconds; the lifetime of BOTH the session cookie's ``Max-Age`` and the
    # server-side registration behind it, refreshed together on every door that
    # resolves the cookie. An idle visitor's session therefore outlives their
    # transcript, whose own TTL is refreshed only on append: a resolving cookie
    # promises a conversation address, never that it still has a backlog.
    session_ttl_seconds: int = Field(default=30 * 86400, gt=0)
    # Seconds; the lifetime a JUST-MINTED registration gets, before its cookie has
    # ever come back. Promoted to ``session_ttl_seconds`` the first time a door
    # resolves it, so a cookie-less GET loop leaves short-lived keys behind instead
    # of one full-TTL registration per request.
    session_pending_ttl_seconds: int = Field(default=600, gt=0)
    # How often ONE question's record may be put back after a refused or failed
    # forward before it is left dropped. Each restore buys another forward, so an
    # uncapped one is an unauthenticated loop over the interactions callback door's
    # own rate limit, which is keyed on this server's egress IP and shared with every
    # other channel's answer forwards.
    max_answer_restores: int = Field(default=5, gt=0)
    # Bytes; the bounded read cap on the POST doors that take a visitor body.
    # Counted as ACTUAL bytes read, never a declared ``Content-Length``; over-cap is
    # a loud 413, never a truncated body.
    max_body_bytes: int = Field(default=65536, gt=0)
    # Concurrent SSE streams admitted for ONE visitor and across this whole process.
    # Each stream pins a dedicated non-pooled Redis connection for its whole life, so
    # both ceilings bound one real resource; over either is a loud 503. Counted per
    # process — a multi-worker deployment admits the total once per worker.
    max_streams_per_visitor: int = Field(default=4, gt=0)
    max_streams_total: int = Field(default=500, gt=0)
    # The chat page's ``<title>`` — the visitor-facing name of the conversation.
    page_title: str = "Chat"
    # Entry-code guesses one client bucket may make against a gated route per window,
    # and the window's length. A gate refuses uniformly (no oracle), so throttling the
    # guess rate is what bounds a brute force over the code space.
    entry_attempts_per_window: int = Field(default=10, gt=0)
    entry_throttle_window_seconds: int = Field(default=300, gt=0)


@settings_cache
def web_settings() -> WebSettings:
    return WebSettings()


class WebRedisSettings(RedisConnectionSettings):
    """Connection for the plugin-owned transcript store (``CHANNEL_WEB_REDIS_URL`` etc.)."""

    model_config = SettingsConfigDict(env_prefix="CHANNEL_WEB_")


@settings_cache
def web_redis_settings() -> WebRedisSettings:
    return WebRedisSettings()
