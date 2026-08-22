"""``claude_code`` settings: exactly-one auth, digest-only image, and the cred models."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, TypeAdapter, ValidationError

from tai42_agents.claude_code.settings import (
    ANTHROPIC_API_KEY_ENV,
    CLAUDE_CODE_OAUTH_TOKEN_ENV,
    ClaudeCodeSettings,
    ConnectionCred,
    SessionCredSpec,
    StaticCred,
)

_DIGEST = "registry.example/claude@sha256:" + "a" * 64


def _settings(**overrides: object) -> ClaudeCodeSettings:
    base: dict[str, object] = {"session_image": _DIGEST, "api_key": SecretStr("k")}
    base.update(overrides)
    return ClaudeCodeSettings(**base)  # type: ignore[arg-type]


def test_api_key_only_is_valid() -> None:
    settings = _settings()
    assert settings.model_credential() == (ANTHROPIC_API_KEY_ENV, settings.api_key)


def test_oauth_token_only_is_valid() -> None:
    settings = _settings(api_key=None, oauth_token=SecretStr("t"))
    env_name, secret = settings.model_credential()
    assert env_name == CLAUDE_CODE_OAUTH_TOKEN_ENV
    assert secret.get_secret_value() == "t"


def test_neither_auth_raises() -> None:
    with pytest.raises(ValidationError, match="EXACTLY ONE model credential"):
        _settings(api_key=None)


def test_both_auth_raises() -> None:
    with pytest.raises(ValidationError, match="EXACTLY ONE model credential"):
        _settings(oauth_token=SecretStr("t"))


def test_bare_tag_image_raises() -> None:
    with pytest.raises(ValidationError, match="digest reference"):
        _settings(session_image="registry.example/claude:latest")


def test_short_digest_raises() -> None:
    with pytest.raises(ValidationError, match="digest reference"):
        _settings(session_image="registry.example/claude@sha256:abc")


def test_valid_digest_accepted() -> None:
    assert _settings(session_image=_DIGEST).session_image == _DIGEST


def test_creds_default_empty() -> None:
    assert _settings().creds == []


def test_session_cred_spec_parses_both_variants() -> None:
    adapter = TypeAdapter(list[SessionCredSpec])
    creds = adapter.validate_python(
        [
            {"kind": "static", "env_name": "GH_TOKEN", "value": "secret"},
            {
                "kind": "connection",
                "env_name": "GH_TOKEN",
                "connection_id": "conn-1",
                "provider_id": "github",
                "sub_service": "api",
            },
        ]
    )
    assert isinstance(creds[0], StaticCred)
    assert isinstance(creds[1], ConnectionCred)
    # Bearer is the default delivery for a refreshable connection cred.
    assert creds[1].delivery == "bearer"
    assert creds[1].required is True


def test_crash_resume_is_recycle_class() -> None:
    field = ClaudeCodeSettings.model_fields["crash_resume"]
    assert field.json_schema_extra == {"reload": "recycle"}
    assert _settings().crash_resume is False
