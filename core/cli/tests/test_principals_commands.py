"""``tai principals`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_principals_list_renders_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/auth/principals"
        return data_response(
            [
                {
                    "user_id": "svc-1",
                    "kind": "service",
                    "display_name": "CI runner",
                    "created_by": "usr-owner",
                    "disabled": False,
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ]
        )

    result = run_cli(monkeypatch, handler, ["principals", "list"])
    assert result.exit_code == 0, result.output
    assert "svc-1" in result.output
    assert "service" in result.output
    assert "disabled" in result.output


def test_principals_create_sends_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/auth/principals"
        assert json.loads(request.content) == {
            "kind": "service",
            "display_name": "CI runner",
            "role": "editor",
            "user_id": "svc-1",
        }
        return data_response({"user_id": "svc-1", "kind": "service"})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "principals",
            "create",
            "--kind",
            "service",
            "--display-name",
            "CI runner",
            "--role",
            "editor",
            "--user",
            "svc-1",
        ],
    )
    assert result.exit_code == 0, result.output


def test_principals_create_omits_user_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "user_id" not in json.loads(request.content)
        return data_response({"user_id": "usr-minted", "kind": "human"})

    result = run_cli(
        monkeypatch,
        handler,
        ["principals", "create", "--kind", "human", "--display-name", "Alice", "--role", "editor"],
    )
    assert result.exit_code == 0, result.output


def test_principals_create_requires_kind_display_name_and_role(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["principals", "create", "--display-name", "Alice", "--role", "editor"])
    assert result.exit_code != 0


def test_principals_create_rejects_unknown_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["principals", "create", "--kind", "robot", "--display-name", "Alice", "--role", "editor"],
    )
    assert result.exit_code != 0


def test_principals_disable_sends_true(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/auth/principals/svc-1"
        assert json.loads(request.content) == {"disabled": True}
        return data_response({"user_id": "svc-1", "disabled": True})

    result = run_cli(monkeypatch, handler, ["principals", "disable", "svc-1"])
    assert result.exit_code == 0, result.output


def test_principals_enable_sends_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/auth/principals/svc-1"
        assert json.loads(request.content) == {"disabled": False}
        return data_response({"user_id": "svc-1", "disabled": False})

    result = run_cli(monkeypatch, handler, ["principals", "enable", "svc-1"])
    assert result.exit_code == 0, result.output


def test_principals_delete_hits_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/auth/principals/svc-1"
        return data_response({"user_id": "svc-1", "deleted": True})

    result = run_cli(monkeypatch, handler, ["principals", "delete", "svc-1"])
    assert result.exit_code == 0, result.output
