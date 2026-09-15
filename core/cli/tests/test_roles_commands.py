"""``tai roles`` commands against a fake server.

Pins the request shaping the roles commands send — in particular that a
description-only ``edit`` OMITS the grant map (so the server keeps the stored grants
rather than wiping them), while an edit that passes ``--grant`` sends the grant map.
"""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, run_cli


def _capture():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content) if request.content else None
        return data_response(captured.get("_payload", {"name": "ops"}))

    return handler, captured


def test_edit_description_only_omits_grants(monkeypatch) -> None:
    # A description-only edit must NOT send ``grants`` — an absent key means "keep the
    # stored grant map" on the server, so the grant map is never silently wiped.
    handler, captured = _capture()
    result = run_cli(monkeypatch, handler, ["roles", "edit", "ops", "--description", "new desc"])
    assert result.exit_code == 0, result.output
    assert captured["method"] == "PUT"
    assert captured["path"] == "/api/auth/roles/ops"
    assert "grants" not in captured["body"]
    assert captured["body"]["description"] == "new desc"


def test_edit_with_grant_sends_grants(monkeypatch) -> None:
    handler, captured = _capture()
    result = run_cli(monkeypatch, handler, ["roles", "edit", "ops", "--grant", "presets=write"])
    assert result.exit_code == 0, result.output
    assert captured["body"]["grants"] == {"presets": "write"}
    assert "description" not in captured["body"]


def test_edit_with_grant_and_description_sends_both(monkeypatch) -> None:
    handler, captured = _capture()
    result = run_cli(monkeypatch, handler, ["roles", "edit", "ops", "--grant", "hooks=read", "--description", "d"])
    assert result.exit_code == 0, result.output
    assert captured["body"]["grants"] == {"hooks": "read"}
    assert captured["body"]["description"] == "d"


def test_grants_set_parses_and_posts(monkeypatch) -> None:
    handler, captured = _capture()
    result = run_cli(monkeypatch, handler, ["roles", "grants", "ops", "--set", "hooks=read"])
    assert result.exit_code == 0, result.output
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/auth/roles/ops/grants"
    assert captured["body"] == {"upsert": {"hooks": "read"}, "remove": []}


def test_grants_set_and_remove_body_mapping(monkeypatch) -> None:
    handler, captured = _capture()
    result = run_cli(monkeypatch, handler, ["roles", "grants", "ops", "--set", "hooks=write", "--remove", "presets"])
    assert result.exit_code == 0, result.output
    assert captured["body"] == {"upsert": {"hooks": "write"}, "remove": ["presets"]}


def test_grants_no_flags_is_bad_parameter(monkeypatch) -> None:
    handler, _ = _capture()
    result = run_cli(monkeypatch, handler, ["roles", "grants", "ops"])
    assert result.exit_code != 0


def test_grants_malformed_set_no_equals_is_bad_parameter(monkeypatch) -> None:
    handler, _ = _capture()
    result = run_cli(monkeypatch, handler, ["roles", "grants", "ops", "--set", "hooks"])
    assert result.exit_code != 0


def test_grants_malformed_set_two_equals_is_bad_parameter(monkeypatch) -> None:
    handler, _ = _capture()
    result = run_cli(monkeypatch, handler, ["roles", "grants", "ops", "--set", "a=b=c"])
    assert result.exit_code != 0


def test_roles_show_filters_the_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/roles"
        return data_response([{"name": "editor"}, {"name": "viewer"}])

    result = run_cli(monkeypatch, handler, ["roles", "show", "editor"])
    assert result.exit_code == 0, result.output
    assert "editor" in result.output


def test_roles_show_unknown_role_is_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, lambda r: data_response([{"name": "editor"}]), ["roles", "show", "nope"])
    assert result.exit_code != 0


def test_roles_create_parses_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["grants"] == {"presets": "write", "hooks": "read"}
        return data_response({"name": "ops"})

    result = run_cli(
        monkeypatch,
        handler,
        ["roles", "create", "ops", "--base-tier", "editor", "--grant", "presets=write", "--grant", "hooks=read"],
    )
    assert result.exit_code == 0, result.output


def test_roles_create_rejects_malformed_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(
        monkeypatch,
        lambda r: data_response({}),
        ["roles", "create", "ops", "--base-tier", "editor", "--grant", "badgrant"],
    )
    assert result.exit_code != 0


def test_roles_edit_sends_only_passed_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/auth/roles/ops"
        assert json.loads(request.content) == {"description": "new"}
        return data_response({"name": "ops"})

    assert run_cli(monkeypatch, handler, ["roles", "edit", "ops", "--description", "new"]).exit_code == 0


def test_roles_versions_and_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    def versions_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/roles/ops/versions"
        return data_response({"versions": []})

    assert run_cli(monkeypatch, versions_handler, ["roles", "versions", "ops"]).exit_code == 0

    def rollback_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/roles/ops/rollback"
        assert json.loads(request.content) == {"version": 2}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, rollback_handler, ["roles", "rollback", "ops", "2"]).exit_code == 0
