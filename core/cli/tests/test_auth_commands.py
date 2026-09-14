"""``tai auth`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli


def test_auth_whoami_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/auth/me"
        assert request.headers["x-api-key"] == "test-key"
        return data_response(
            {
                "user_id": "u1",
                "owner_user_id": None,
                "admin": False,
                "scopes": ["read"],
                "routes": [],
                "route_patterns": [],
                "sub_mcp": [],
                "tools": [],
                "agents": [],
                "mintable": True,
            }
        )

    result = run_cli(monkeypatch, handler, ["auth", "whoami"])
    assert result.exit_code == 0, result.output
    assert "u1" in result.output


def test_auth_whoami_json_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response({"user_id": "u1", "admin": True, "scopes": ["*"]})

    result = run_cli(monkeypatch, handler, ["auth", "whoami"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["admin"] is True


def test_auth_whoami_error_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("Unauthorized", 401)

    result = run_cli(monkeypatch, handler, ["auth", "whoami"])
    assert result.exit_code != 0
    assert "Unauthorized" in result.output


def test_auth_claim_sends_no_credential_and_extracts_token_from_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/login/claim"
        # The public exchange carries NO credential header.
        assert "x-api-key" not in request.headers
        assert "authorization" not in request.headers
        # A full pasted claim URL is reduced to its bare fragment token.
        assert json.loads(request.content) == {"token": "clm-abc123"}
        return data_response({"token": "sk-live", "user_id": "u1"})

    result = run_cli(monkeypatch, handler, ["auth", "claim", "https://host/login#claim=clm-abc123"])
    assert result.exit_code == 0, result.output
    assert "sk-live" in result.output


def test_auth_claim_accepts_a_bare_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"token": "clm-bare"}
        return data_response({"token": "sk-live", "user_id": "u1"})

    result = run_cli(monkeypatch, handler, ["auth", "claim", "clm-bare"])
    assert result.exit_code == 0, result.output


def test_auth_claim_404_renders_uniform_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("unknown or already used claim token", 404)

    result = run_cli(monkeypatch, handler, ["auth", "claim", "clm-nope"])
    assert result.exit_code != 0
    assert "unknown or already used claim token" in result.output


def test_auth_claim_reads_token_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/login/claim"
        seen.update(json.loads(request.content))
        return data_response({"api_key": "sk-x"})

    result = run_cli(monkeypatch, handler, ["auth", "claim", "-"], stdin="  tok-123  \n")
    assert result.exit_code == 0, result.output
    assert seen["token"] == "tok-123"


def test_auth_claim_reads_url_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return data_response({"api_key": "sk-x"})

    result = run_cli(monkeypatch, handler, ["auth", "claim", "-"], stdin="https://host/login#claim=tok-123\n")
    assert result.exit_code == 0, result.output
    assert seen["token"] == "tok-123"
