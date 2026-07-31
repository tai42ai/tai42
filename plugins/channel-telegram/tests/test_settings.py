"""Env mapping, secret masking, and the loud require helpers."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError
from tai42_kit.settings import reset_all_settings

from tai42_channel_telegram.settings import (
    TelegramSettings,
    bot_numeric_id,
    require,
    require_secret,
    telegram_correlation_settings,
    telegram_settings,
)


def test_bot_numeric_id_extracts_prefix():
    assert bot_numeric_id("123456:test-token") == "123456"


@pytest.mark.parametrize("token", ["123456", "notnumeric:secret", "", ":secret"])
def test_bot_numeric_id_malformed_token_raises(token: str):
    # No ``:`` separator or a non-numeric prefix is a misconfiguration, never a
    # silent default.
    with pytest.raises(ValueError, match="CHANNEL_TELEGRAM_BOT_TOKEN is malformed"):
        bot_numeric_id(token)


def test_env_mapping():
    settings = telegram_settings()
    assert settings.bot_token is not None
    assert settings.bot_token.get_secret_value() == "123456:test-token"
    assert settings.default_recipient == "777"
    assert settings.allowed_recipients == ["888", "999"]
    assert settings.webhook_secret is not None
    assert settings.webhook_secret.get_secret_value() == "s3cret_token"
    assert settings.public_base_url == "https://example.test"
    assert settings.api_base_url == "https://api.telegram.org"
    assert settings.http_timeout_seconds == 30


def test_allowed_recipients_comma_string_split_strip_drop_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS", " -1001234567890 ,@ops_bot, ,888,")
    reset_all_settings()
    assert telegram_settings().allowed_recipients == ["-1001234567890", "@ops_bot", "888"]


def test_allowed_recipients_unset_defaults_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS")
    reset_all_settings()
    assert telegram_settings().allowed_recipients == []


def test_allowed_recipients_json_list_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS", '["888","999"]')
    reset_all_settings()
    assert telegram_settings().allowed_recipients == ["888", "999"]


def test_allowed_recipients_json_string_direct():
    settings = TelegramSettings(allowed_recipients='["-1001234567890", "@ops_bot"]')  # pyright: ignore[reportArgumentType]
    assert settings.allowed_recipients == ["-1001234567890", "@ops_bot"]


def test_allowed_recipients_json_items_stripped_empties_dropped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS", ' [" 888 ", "@ops_bot", "", "  "] ')
    reset_all_settings()
    assert telegram_settings().allowed_recipients == ["888", "@ops_bot"]


def test_allowed_recipients_malformed_json_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS", '["888", "999"')
    reset_all_settings()
    with pytest.raises(ValidationError, match="allowed_recipients"):
        TelegramSettings()


def test_allowed_recipients_json_non_string_entry_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS", '["888", 999]')
    reset_all_settings()
    with pytest.raises(ValidationError, match="entries must be strings"):
        TelegramSettings()


def test_allowed_recipients_list_normalized():
    settings = TelegramSettings(allowed_recipients=[" 777 ", "@ops_bot", ""])
    assert settings.allowed_recipients == ["777", "@ops_bot"]


def test_allowed_recipients_other_shape_raises():
    with pytest.raises(ValidationError, match="comma-separated string or a list"):
        TelegramSettings(allowed_recipients=777)  # pyright: ignore[reportArgumentType]


def test_secrets_masked_in_repr_and_str():
    settings = telegram_settings()
    for rendered in (repr(settings), str(settings)):
        assert "123456:test-token" not in rendered
        assert "s3cret_token" not in rendered


def test_correlation_settings_client_kwargs():
    kwargs = telegram_correlation_settings().client_kwargs()
    assert kwargs["url"] == "redis://localhost:6379/0"
    assert kwargs["decode_responses"] is True


def test_require_returns_configured_value():
    assert require("777", "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT") == "777"


def test_require_raises_naming_env_var():
    with pytest.raises(ValueError, match="set CHANNEL_TELEGRAM_DEFAULT_RECIPIENT"):
        require(None, "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT")


def test_require_secret_returns_plaintext():
    assert require_secret(SecretStr("tok"), "CHANNEL_TELEGRAM_BOT_TOKEN") == "tok"


def test_require_secret_unset_raises():
    with pytest.raises(ValueError, match="set CHANNEL_TELEGRAM_BOT_TOKEN"):
        require_secret(None, "CHANNEL_TELEGRAM_BOT_TOKEN")


def test_require_secret_empty_raises_fail_closed():
    with pytest.raises(ValueError, match="CHANNEL_TELEGRAM_WEBHOOK_SECRET is set but empty"):
        require_secret(SecretStr(""), "CHANNEL_TELEGRAM_WEBHOOK_SECRET")


def test_zero_timeout_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_TELEGRAM_HTTP_TIMEOUT_SECONDS", "0")
    reset_all_settings()
    with pytest.raises(ValidationError, match="http_timeout_seconds"):
        TelegramSettings()
