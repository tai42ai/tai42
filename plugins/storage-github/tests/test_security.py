"""The token must reach the wire but never a log line or error message."""

from __future__ import annotations

import logging

import httpx
import pytest
from httpx import HTTPStatusError

from tai42_storage_github import GithubStorage
from tests.conftest import make_response

pytestmark = pytest.mark.usefixtures("client")


async def test_token_sent_on_wire_but_not_leaked(client, settings, caplog):
    client.get.return_value = make_response(status=500, text="server said no")
    secret = settings.token.get_secret_value()

    with caplog.at_level(logging.ERROR), pytest.raises(HTTPStatusError) as exc:
        await GithubStorage().load("x.j2")

    # The token authenticated the request...
    assert client.get.call_args.kwargs["headers"]["Authorization"] == f"Bearer {secret}"
    # ...but never appears in the raised error or the logs.
    assert secret not in str(exc.value)
    assert secret not in caplog.text


async def test_public_repo_omits_authorization_header(client, settings):
    settings.token = None
    client.get.return_value = make_response(status=200, text="public content")

    await GithubStorage().load("x.j2")

    assert "Authorization" not in client.get.call_args.kwargs["headers"]


async def test_settings_secretstr_hides_token_in_repr(monkeypatch):
    monkeypatch.setenv("STORAGE_GITHUB_TOKEN", "super-secret-token")
    from tai42_storage_github.settings import GithubStorageSettings

    loaded = GithubStorageSettings()

    assert loaded.token is not None
    assert loaded.token.get_secret_value() == "super-secret-token"
    assert "super-secret-token" not in repr(loaded)
    assert "super-secret-token" not in str(loaded)


async def test_client_create_and_close(monkeypatch):
    from types import SimpleNamespace

    from tai42_storage_github import client as client_mod

    monkeypatch.setattr(
        client_mod,
        "github_storage_settings",
        lambda: SimpleNamespace(
            timeout_total=5.0,
            max_connections=10,
            max_keepalive_connections=5,
            keepalive_expiry=30.0,
        ),
    )
    pooled = client_mod.GithubHttpxClient()
    http = await pooled._create()

    assert isinstance(http, httpx.AsyncClient)

    await pooled._close(http)
    assert http.is_closed
