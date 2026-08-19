"""Settings: env round-trips, secret hygiene, and the fail-closed require helpers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tai42_kit.settings import require_secret, reset_all_settings

from tai42_channel_slack.settings import (
    SlackRedisSettings,
    SlackSettings,
    slack_redis_settings,
    slack_settings,
)

from .conftest import (
    TEST_ALLOWED_RECIPIENT,
    TEST_BOT_TOKEN,
    TEST_BOT_USER_ID,
    TEST_DEFAULT_RECIPIENT,
    TEST_SIGNING_SECRET,
)


def test_env_round_trip(slack_env):
    settings = SlackSettings()
    assert settings.bot_token is not None
    assert settings.bot_token.get_secret_value() == TEST_BOT_TOKEN
    assert settings.signing_secret is not None
    assert settings.signing_secret.get_secret_value() == TEST_SIGNING_SECRET
    assert settings.allowed_recipients == [TEST_ALLOWED_RECIPIENT]
    assert settings.default_recipient == TEST_DEFAULT_RECIPIENT
    assert settings.bot_user_id == TEST_BOT_USER_ID


def test_secrets_never_surface_in_repr_or_dump(slack_env):
    settings = SlackSettings()
    for exposed in (repr(settings), str(settings.model_dump())):
        assert TEST_BOT_TOKEN not in exposed
        assert TEST_SIGNING_SECRET not in exposed


def test_no_env_constructs_cleanly_all_unset(no_slack_env):
    # A library import never demands configuration: every credential/recipient
    # field defaults unset.
    settings = SlackSettings()
    assert settings.bot_token is None
    assert settings.signing_secret is None
    assert settings.bot_user_id is None
    assert settings.allowed_recipients == []
    assert settings.default_recipient is None


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [
        pytest.param("C0AAA", ["C0AAA"], id="single-id"),
        pytest.param(" C0AAA , C0BBB ,,D0CCC,", ["C0AAA", "C0BBB", "D0CCC"], id="comma-separated-messy"),
        pytest.param('["C0AAA", "D0BBB"]', ["C0AAA", "D0BBB"], id="json-list"),
        pytest.param('[" C0AAA ", "", " D0BBB"]', ["C0AAA", "D0BBB"], id="json-list-padded-and-empty"),
    ],
)
def test_allowed_recipients_env_forms(no_slack_env, monkeypatch, env_value, expected):
    monkeypatch.setenv("CHANNEL_SLACK_ALLOWED_RECIPIENTS", env_value)
    reset_all_settings()
    assert SlackSettings().allowed_recipients == expected


def test_allowed_recipients_json_padding_never_blocks_a_clean_caller_id(no_slack_env, monkeypatch):
    # A JSON entry padded by the operator must still admit the recipient a
    # caller names without padding — the exact membership check delivery runs.
    monkeypatch.setenv("CHANNEL_SLACK_ALLOWED_RECIPIENTS", '[" C0AAA "]')
    reset_all_settings()
    assert "C0AAA" in set(SlackSettings().allowed_recipients)


def test_allowed_recipients_accepts_a_list_directly(no_slack_env):
    assert SlackSettings(allowed_recipients=["C0AAA"]).allowed_recipients == ["C0AAA"]


def test_allowed_recipients_malformed_json_env_raises(no_slack_env, monkeypatch):
    monkeypatch.setenv("CHANNEL_SLACK_ALLOWED_RECIPIENTS", "[not json")
    reset_all_settings()
    with pytest.raises(ValidationError, match="allowed_recipients"):
        SlackSettings()


def test_allowed_recipients_json_non_string_items_raise(no_slack_env, monkeypatch):
    monkeypatch.setenv("CHANNEL_SLACK_ALLOWED_RECIPIENTS", "[1, 2]")
    reset_all_settings()
    with pytest.raises(ValidationError, match="allowed_recipients"):
        SlackSettings()


def test_allowed_recipients_rejects_other_types_loudly(no_slack_env):
    with pytest.raises(ValidationError, match="comma-separated string or a list"):
        SlackSettings.model_validate({"allowed_recipients": 42})


def test_empty_env_secret_is_treated_as_missing(no_slack_env, monkeypatch):
    # env_ignore_empty: an empty CHANNEL_SLACK_SIGNING_SECRET= leaves the field
    # None, so require_secret raises through the same fail-closed helper.
    monkeypatch.setenv("CHANNEL_SLACK_SIGNING_SECRET", "")
    reset_all_settings()
    settings = SlackSettings()
    assert settings.signing_secret is None
    with pytest.raises(ValueError, match="CHANNEL_SLACK_SIGNING_SECRET"):
        require_secret(settings.signing_secret, "the slack channel", "CHANNEL_SLACK_SIGNING_SECRET")


def test_api_base_url_default_and_override(no_slack_env, monkeypatch):
    # Production default is Slack's Web API origin; an operator (or the e2e
    # harness pointing at a stub) may override it.
    assert SlackSettings().api_base_url == "https://slack.com/api"
    monkeypatch.setenv("CHANNEL_SLACK_API_BASE_URL", "http://127.0.0.1:9099/api")
    reset_all_settings()
    assert SlackSettings().api_base_url == "http://127.0.0.1:9099/api"


def test_http_timeout_default_and_override(no_slack_env, monkeypatch):
    assert SlackSettings().http_timeout_seconds == 30
    monkeypatch.setenv("CHANNEL_SLACK_HTTP_TIMEOUT_SECONDS", "7.5")
    reset_all_settings()
    assert SlackSettings().http_timeout_seconds == 7.5


def test_http_timeout_rejects_non_positive(no_slack_env, monkeypatch):
    monkeypatch.setenv("CHANNEL_SLACK_HTTP_TIMEOUT_SECONDS", "0")
    reset_all_settings()
    with pytest.raises(ValidationError, match="http_timeout_seconds"):
        SlackSettings()


def test_redis_settings_read_url_and_client_kwargs(slack_env):
    settings = SlackRedisSettings()
    assert settings.redis_url == "redis://correlation-store:6379/0"
    assert settings.client_kwargs()["url"] == "redis://correlation-store:6379/0"
    # decode_responses default True: the correlation getters return str.
    assert settings.client_kwargs()["decode_responses"] is True


def test_cached_accessors_reset_with_settings_cache(slack_env, monkeypatch):
    assert slack_settings() is slack_settings()
    assert slack_redis_settings() is slack_redis_settings()
    before = slack_settings()
    monkeypatch.setenv("CHANNEL_SLACK_DEFAULT_RECIPIENT", "C0ROTATED")
    reset_all_settings()
    after = slack_settings()
    assert after is not before
    assert after.default_recipient == "C0ROTATED"
