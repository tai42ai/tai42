"""``tai roles`` commands against a fake server.

Pins the request shaping the roles commands send — in particular that a
description-only ``edit`` OMITS the grant map (so the server keeps the stored grants
rather than wiping them), while an edit that passes ``--grant`` sends the grant map.
"""

from __future__ import annotations

import json

import httpx

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
    assert captured["body"] == {"set": {"hooks": "read"}, "remove": []}


def test_grants_set_and_remove_body_mapping(monkeypatch) -> None:
    handler, captured = _capture()
    result = run_cli(monkeypatch, handler, ["roles", "grants", "ops", "--set", "hooks=write", "--remove", "presets"])
    assert result.exit_code == 0, result.output
    assert captured["body"] == {"set": {"hooks": "write"}, "remove": ["presets"]}


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
