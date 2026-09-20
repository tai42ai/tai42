"""Validator tests for ``connectors/models.py`` — the connection record, connector ref,
and the request models that cap ``enabled_sub_services``."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import BaseModel

from tai42_contract.connectors.models import (
    ConnectionRecord,
    ConnectorRef,
    PatchSubServicesRequest,
    StartConnectRequest,
    StartReconnectRequest,
)

UUID = "12345678-1234-1234-1234-123456789ABC"

# === connectors/models.py ===================================================


def _now() -> datetime:
    return datetime.now(UTC)


def _oauth_record(**overrides: Any) -> ConnectionRecord:
    base: dict[str, Any] = {
        "connection_id": UUID,
        "provider_id": "google",
        "alias": "my-google",
        "kind": "oauth",
        "account_identity": "me@example.com",
        "enabled_sub_services": ["gmail"],
        "access_token": "a-token",
        "refresh_token": "r-token",
        "access_token_expires_at": _now(),
        "created_at": _now(),
    }
    base.update(overrides)
    return ConnectionRecord(**base)


def _noauth_record(**overrides: Any) -> ConnectionRecord:
    base: dict[str, Any] = {
        "connection_id": UUID,
        "provider_id": "local",
        "alias": "files",
        "kind": "none",
        "enabled_sub_services": ["files"],
        "config_values": {"api_key": "v"},
        "created_at": _now(),
    }
    base.update(overrides)
    return ConnectionRecord(**base)


def test_record_connection_id_normalized_lowercase():
    assert _oauth_record().connection_id == UUID.lower()


def test_record_connection_id_invalid_raises():
    with pytest.raises(ValueError, match="not a valid UUID"):
        _oauth_record(connection_id="not-a-uuid")


def test_record_provider_id_slug_invalid_raises():
    with pytest.raises(ValueError, match="lowercase"):
        _oauth_record(provider_id="Google")


def test_record_alias_invalid_raises():
    with pytest.raises(ValueError, match="alias must be"):
        _oauth_record(alias="-bad")


def test_record_enabled_sub_services_slug_invalid_raises():
    with pytest.raises(ValueError, match="lowercase"):
        _oauth_record(enabled_sub_services=["Bad"])


def test_record_naive_datetime_raises():
    with pytest.raises(ValueError, match="timezone-aware"):
        _oauth_record(created_at=datetime(2026, 1, 1))


def test_record_datetime_converted_to_utc():
    plus2 = timezone(timedelta(hours=2))
    rec = _oauth_record(created_at=datetime(2026, 1, 1, 12, 0, tzinfo=plus2))
    assert rec.created_at.tzinfo == UTC
    assert rec.created_at.hour == 10


def test_record_oauth_valid():
    assert _oauth_record().kind == "oauth"


def test_record_oauth_requires_tokens():
    with pytest.raises(ValueError, match="access \\+ refresh tokens"):
        _oauth_record(access_token=None)


def test_record_oauth_requires_account_identity():
    with pytest.raises(ValueError, match="requires account_identity"):
        _oauth_record(account_identity=None)


def test_record_oauth_requires_expires_at():
    with pytest.raises(ValueError, match="access_token_expires_at"):
        _oauth_record(access_token_expires_at=None)


def test_record_oauth_forbids_config_values():
    with pytest.raises(ValueError, match="must not carry config_values"):
        _oauth_record(config_values={"k": "v"})


def test_record_noauth_valid():
    assert _noauth_record().kind == "none"


def test_record_noauth_forbids_tokens_identity_expiry():
    with pytest.raises(ValueError, match="must not carry tokens"):
        _noauth_record(access_token="x")
    with pytest.raises(ValueError, match="must not carry tokens"):
        _noauth_record(account_identity="me@x.com")
    with pytest.raises(ValueError, match="must not carry tokens"):
        _noauth_record(access_token_expires_at=_now())


# -- ConnectorRef ------------------------------------------------------------


def test_connector_ref_valid():
    ref = ConnectorRef(connection_id=UUID, provider_id="google", sub_service="gmail")
    assert ref.connection_id == UUID.lower()


def test_connector_ref_uuid_invalid_raises():
    with pytest.raises(ValueError, match="not a valid UUID"):
        ConnectorRef(connection_id="nope", provider_id="google", sub_service="gmail")


def test_connector_ref_slug_invalid_raises():
    with pytest.raises(ValueError, match="lowercase"):
        ConnectorRef(connection_id=UUID, provider_id="Google", sub_service="gmail")
    with pytest.raises(ValueError, match="lowercase"):
        ConnectorRef(connection_id=UUID, provider_id="google", sub_service="Gmail")


# === connectors — request models cap enabled_sub_services at 64 =============


def _build_start_connect(subs: list[str]) -> BaseModel:
    return StartConnectRequest(provider_id="google", alias="a", enabled_sub_services=subs)


def _build_start_reconnect(subs: list[str]) -> BaseModel:
    return StartReconnectRequest(enabled_sub_services=subs)


def _build_patch_sub_services(subs: list[str]) -> BaseModel:
    return PatchSubServicesRequest(enabled_sub_services=subs)


@pytest.mark.parametrize(
    "build",
    [_build_start_connect, _build_start_reconnect, _build_patch_sub_services],
)
def test_request_enabled_sub_services_length_bounds(build: Callable[[list[str]], BaseModel]):
    # min_length=1 and max_length=64 both enforced at the request layer, so an
    # oversize list is rejected with 422 instead of exploding at record build.
    assert build([f"s{i}" for i in range(64)])
    with pytest.raises(ValueError, match="at most 64"):
        build([f"s{i}" for i in range(65)])
    with pytest.raises(ValueError, match="at least 1"):
        build([])
