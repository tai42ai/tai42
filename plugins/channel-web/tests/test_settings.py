"""Settings: env round-trips and bounded numerics (the store connection's own
fail-closed guard is exercised in ``test_store``, which owns it)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tai42_kit.settings import reset_all_settings

from tai42_channel_web.settings import (
    WebRedisSettings,
    WebSettings,
    web_redis_settings,
    web_settings,
)


def test_defaults_construct_cleanly(no_web_env):
    # A library import never demands configuration: every field carries a safe default.
    settings = WebSettings()
    assert settings.http_timeout_seconds == 30.0
    assert settings.transcript_max_entries == 1000
    assert settings.transcript_ttl_seconds == 30 * 86400
    assert settings.blocking_grace_seconds == 5.0
    assert settings.keepalive_seconds == 15
    # A public cookie is Secure by default; the plaintext-origin opt-out is explicit.
    assert settings.session_cookie_secure is True
    assert settings.session_ttl_seconds == settings.transcript_ttl_seconds
    assert settings.max_body_bytes == 65536
    assert settings.max_streams_per_visitor == 4
    assert settings.max_streams_total == 500
    assert settings.page_title == "Chat"
    assert settings.backlog_batch_entries == 200
    # A fresh mint is short-lived until its cookie comes back.
    assert settings.session_pending_ttl_seconds == 600
    assert settings.session_pending_ttl_seconds < settings.session_ttl_seconds
    # A refused answer may be re-answered, but never in an unbounded loop.
    assert settings.max_answer_restores == 5


def test_env_override(no_web_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_WEB_HTTP_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("CHANNEL_WEB_TRANSCRIPT_MAX_ENTRIES", "50")
    monkeypatch.setenv("CHANNEL_WEB_TRANSCRIPT_TTL_SECONDS", "3600")
    monkeypatch.setenv("CHANNEL_WEB_KEEPALIVE_SECONDS", "5")
    monkeypatch.setenv("CHANNEL_WEB_BLOCKING_GRACE_SECONDS", "2.5")
    monkeypatch.setenv("CHANNEL_WEB_SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("CHANNEL_WEB_SESSION_TTL_SECONDS", "600")
    monkeypatch.setenv("CHANNEL_WEB_MAX_BODY_BYTES", "2048")
    monkeypatch.setenv("CHANNEL_WEB_MAX_STREAMS_PER_VISITOR", "2")
    monkeypatch.setenv("CHANNEL_WEB_MAX_STREAMS_TOTAL", "7")
    monkeypatch.setenv("CHANNEL_WEB_PAGE_TITLE", "Support")
    reset_all_settings()
    settings = WebSettings()
    assert settings.max_body_bytes == 2048
    assert settings.max_streams_per_visitor == 2
    assert settings.max_streams_total == 7
    assert settings.http_timeout_seconds == 7.5
    assert settings.transcript_max_entries == 50
    assert settings.transcript_ttl_seconds == 3600
    assert settings.keepalive_seconds == 5
    assert settings.blocking_grace_seconds == 2.5
    assert settings.session_cookie_secure is False
    assert settings.session_ttl_seconds == 600
    assert settings.page_title == "Support"


def test_backlog_and_answer_env_override(no_web_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_WEB_BACKLOG_BATCH_ENTRIES", "25")
    monkeypatch.setenv("CHANNEL_WEB_SESSION_PENDING_TTL_SECONDS", "90")
    monkeypatch.setenv("CHANNEL_WEB_MAX_ANSWER_RESTORES", "2")
    reset_all_settings()
    settings = WebSettings()
    assert settings.backlog_batch_entries == 25
    assert settings.session_pending_ttl_seconds == 90
    assert settings.max_answer_restores == 2


@pytest.mark.parametrize(
    ("env_var", "value"),
    [
        ("CHANNEL_WEB_HTTP_TIMEOUT_SECONDS", "0"),
        ("CHANNEL_WEB_TRANSCRIPT_MAX_ENTRIES", "0"),
        ("CHANNEL_WEB_TRANSCRIPT_TTL_SECONDS", "0"),
        ("CHANNEL_WEB_BLOCKING_GRACE_SECONDS", "0"),
        ("CHANNEL_WEB_KEEPALIVE_SECONDS", "0"),
        ("CHANNEL_WEB_SESSION_TTL_SECONDS", "0"),
        ("CHANNEL_WEB_MAX_BODY_BYTES", "0"),
        ("CHANNEL_WEB_MAX_STREAMS_PER_VISITOR", "0"),
        ("CHANNEL_WEB_MAX_STREAMS_TOTAL", "0"),
        ("CHANNEL_WEB_BACKLOG_BATCH_ENTRIES", "0"),
        ("CHANNEL_WEB_SESSION_PENDING_TTL_SECONDS", "0"),
        ("CHANNEL_WEB_MAX_ANSWER_RESTORES", "0"),
    ],
)
def test_non_positive_numeric_rejected(no_web_env, monkeypatch: pytest.MonkeyPatch, env_var: str, value: str):
    monkeypatch.setenv(env_var, value)
    reset_all_settings()
    with pytest.raises(ValidationError):
        WebSettings()


def test_redis_settings_read_url_and_client_kwargs(web_env):
    settings = WebRedisSettings()
    assert settings.redis_url == "redis://test/0"
    assert settings.client_kwargs()["url"] == "redis://test/0"
    # decode_responses default True: the store getters return str.
    assert settings.client_kwargs()["decode_responses"] is True


def test_cached_accessors_reset_with_settings_cache(web_env, monkeypatch: pytest.MonkeyPatch):
    assert web_settings() is web_settings()
    assert web_redis_settings() is web_redis_settings()
    before = web_settings()
    monkeypatch.setenv("CHANNEL_WEB_KEEPALIVE_SECONDS", "9")
    reset_all_settings()
    after = web_settings()
    assert after is not before
    assert after.keepalive_seconds == 9
