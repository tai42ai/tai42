"""Unit tests for the pooled ``S3Client`` with ``aioboto3.Session`` faked.

Covers endpoint-scheme inference, that ``_create`` enters the client context and
returns the live client, and that ``_close`` closes any client it is handed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from tai42_storage_s3 import client as client_module
from tai42_storage_s3.client import S3Client


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "endpoint": None,
        "access_key": SecretStr("ak"),
        "secret_key": SecretStr("sk"),
        "secure": True,
        "region": "us-east-1",
        "verify_ssl": True,
        "connect_timeout": 5,
        "read_timeout": 30,
        "addressing_style": "auto",
        "request_checksum_calculation": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeContext:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    last: _FakeSession | None = None

    def __init__(self) -> None:
        self.client_kwargs: dict[str, Any] | None = None
        self.entered_client = object()
        _FakeSession.last = self

    def client(self, service: str, **kwargs: Any) -> _FakeContext:
        self.client_kwargs = {"service": service, **kwargs}
        return _FakeContext(self.entered_client)


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSession]:
    _FakeSession.last = None
    monkeypatch.setattr(client_module.aioboto3, "Session", _FakeSession)
    return _FakeSession


def _captured_kwargs(fake_session: type[_FakeSession]) -> dict[str, Any]:
    """The kwargs the last fake session's ``client()`` was called with."""
    assert fake_session.last is not None
    assert fake_session.last.client_kwargs is not None
    return fake_session.last.client_kwargs


async def test_create_returns_entered_client(fake_session: type[_FakeSession], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "s3_settings", _settings)

    result = await S3Client()._create()

    assert fake_session.last is not None
    assert result is fake_session.last.entered_client
    kwargs = _captured_kwargs(fake_session)
    assert kwargs["service"] == "s3"
    assert kwargs["endpoint_url"] is None
    # The SecretStr credentials are unwrapped to plaintext for the client.
    assert kwargs["aws_access_key_id"] == "ak"
    assert kwargs["aws_secret_access_key"] == "sk"


async def test_create_passes_none_credentials_when_unset(
    fake_session: type[_FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no configured credentials, None is passed through so boto falls back
    # to its own credential chain.
    monkeypatch.setattr(client_module, "s3_settings", lambda: _settings(access_key=None, secret_key=None))

    await S3Client()._create()

    kwargs = _captured_kwargs(fake_session)
    assert kwargs["aws_access_key_id"] is None
    assert kwargs["aws_secret_access_key"] is None


async def test_create_rejects_unknown_connection_kwargs() -> None:
    # An unrecognized kwarg would split the pool key, so it is rejected loudly.
    with pytest.raises(ValueError, match="unknown connection kwarg"):
        await S3Client()._create(bucket="typo")


async def test_create_passes_connection_config(
    fake_session: type[_FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        client_module,
        "s3_settings",
        lambda: _settings(
            secure=False,
            region="eu-west-1",
            verify_ssl=False,
            connect_timeout=7,
            read_timeout=42,
            addressing_style="path",
            request_checksum_calculation="when_required",
        ),
    )

    await S3Client()._create()

    kwargs = _captured_kwargs(fake_session)
    assert kwargs["region_name"] == "eu-west-1"
    assert kwargs["verify"] is False
    assert kwargs["use_ssl"] is False
    config = kwargs["config"]
    assert config.connect_timeout == 7
    assert config.read_timeout == 42
    assert config.s3 == {"addressing_style": "path"}
    assert config.request_checksum_calculation == "when_required"


async def test_create_infers_https_endpoint(fake_session: type[_FakeSession], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "s3_settings", lambda: _settings(endpoint="minio:9000", secure=True))

    await S3Client()._create()

    assert _captured_kwargs(fake_session)["endpoint_url"] == "https://minio:9000"


async def test_create_infers_http_endpoint(fake_session: type[_FakeSession], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "s3_settings", lambda: _settings(endpoint="minio:9000", secure=False))

    await S3Client()._create()

    assert _captured_kwargs(fake_session)["endpoint_url"] == "http://minio:9000"


async def test_create_keeps_explicit_scheme(fake_session: type[_FakeSession], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "s3_settings", lambda: _settings(endpoint="http://minio:9000"))

    await S3Client()._create()

    assert _captured_kwargs(fake_session)["endpoint_url"] == "http://minio:9000"


async def test_create_prefixes_schemeless_host_starting_with_http(
    fake_session: type[_FakeSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A scheme-less host whose name starts with "http" must still get a scheme.
    monkeypatch.setattr(client_module, "s3_settings", lambda: _settings(endpoint="httpd-internal:9000", secure=False))

    await S3Client()._create()

    assert _captured_kwargs(fake_session)["endpoint_url"] == "http://httpd-internal:9000"


def test_disconnection_predicate_matches_botocore_network_errors() -> None:
    from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

    client = S3Client()

    # botocore's network-level errors evict the pooled client...
    assert client._is_disconnection_error(EndpointConnectionError(endpoint_url="https://s3.local"))
    assert client._is_disconnection_error(ConnectTimeoutError(endpoint_url="https://s3.local"))
    assert client._is_disconnection_error(ReadTimeoutError(endpoint_url="https://s3.local"))
    # ...as does the loop-bound RuntimeError a dead aiohttp transport surfaces.
    assert client._is_disconnection_error(RuntimeError("Event loop is closed"))

    # An API error over a healthy connection and unrelated caller errors do not.
    api_error = ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
    assert not client._is_disconnection_error(api_error)
    assert not client._is_disconnection_error(RuntimeError("caller bug"))
    assert not client._is_disconnection_error(ValueError("caller bug"))


async def test_close_closes_the_client_it_is_handed() -> None:
    # A fresh S3Client must close any client it is handed, keeping no per-instance state.
    client = AsyncMock()

    await S3Client()._close(client)

    client.close.assert_awaited_once_with()
