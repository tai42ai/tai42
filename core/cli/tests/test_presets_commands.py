"""``tai presets`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import Handler, data_response, run_cli


def test_presets_create_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/presets"
        body = json.loads(request.content)
        assert body["name"] == "greet"
        assert body["base_tool"] == "echo"
        assert body["fixed_kwargs"] == {"prefix": "hi"}
        # ``description`` is required on every create and rides the body verbatim.
        assert body["description"] == "Greet a user"
        # Every create is versioned; the body carries no ``versioned`` key.
        assert "versioned" not in body
        return data_response({"name": "greet", "persisted": True})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "presets",
            "create",
            "greet",
            "--base-tool",
            "echo",
            "--description",
            "Greet a user",
            "--kwargs",
            '{"prefix":"hi"}',
        ],
    )
    assert result.exit_code == 0, result.output


def test_presets_create_requires_description(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["presets", "create", "greet", "--base-tool", "echo"])
    assert result.exit_code != 0


def test_presets_save_version_requires_a_field(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["presets", "save-version", "greet"])
    assert result.exit_code != 0
    assert "at least one" in result.output


def test_presets_save_version_sets_description(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"description": "Updated"}
        return data_response({"version": 2})

    result = run_cli(monkeypatch, handler, ["presets", "save-version", "greet", "--description", "Updated"])
    assert result.exit_code == 0, result.output


def test_presets_list_sends_no_tier_param(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets"
        # The list is versioned-only (store-backed); no tier param is sent.
        assert "tier" not in request.url.params
        return data_response([{"name": "greet", "base_tool": "echo", "active_version": 1}])

    result = run_cli(monkeypatch, handler, ["presets", "list"])
    assert result.exit_code == 0, result.output
    assert "greet" in result.output


def test_presets_get_renders_record(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/greet"
        return data_response({"name": "greet", "base_tool": "echo"})

    result = run_cli(monkeypatch, handler, ["presets", "get", "greet"])
    assert result.exit_code == 0, result.output
    assert "echo" in result.output


def test_presets_create_parses_extension_combos(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["extensions"] == [["chain", "batch"], ["chain"]]
        return data_response({"name": "greet", "persisted": True})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "presets",
            "create",
            "greet",
            "--base-tool",
            "echo",
            "--description",
            "Greet a user",
            "--extensions",
            '[["chain","batch"],["chain"]]',
        ],
    )
    assert result.exit_code == 0, result.output


def test_presets_create_carries_element_config_losslessly(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = {"type": "object"}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["extensions"] == [
            ["chain", {"name": "output_schema", "config": {"schema": schema}}],
        ]
        return data_response({"name": "greet", "persisted": True})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "presets",
            "create",
            "greet",
            "--base-tool",
            "echo",
            "--description",
            "Greet a user",
            "--extensions",
            '[["chain",{"name":"output_schema","config":{"schema":{"type":"object"}}}]]',
        ],
    )
    assert result.exit_code == 0, result.output


def test_presets_create_rejects_malformed_extensions_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["presets", "create", "greet", "--base-tool", "echo", "--description", "d", "--extensions", "not json"],
    )
    assert result.exit_code != 0
    assert "valid JSON" in result.output


def test_presets_create_rejects_flat_combo_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    # A flat array of names is not a list OF COMBOS — each combo must itself be an array.
    result = run_cli(
        monkeypatch,
        handler,
        ["presets", "create", "greet", "--base-tool", "echo", "--description", "d", "--extensions", '["chain"]'],
    )
    assert result.exit_code != 0
    assert "non-empty JSON array" in result.output


def test_presets_create_rejects_empty_inner_combo(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty inner combo ``[[]]`` names no extension -> parse_extension_combos
    rejects it via the ``not combo`` sub-condition (distinct from the not-a-list one)."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["presets", "create", "greet", "--base-tool", "echo", "--description", "d", "--extensions", "[[]]"],
    )
    assert result.exit_code != 0
    assert "non-empty JSON array" in result.output


def test_presets_create_rejects_non_list_extensions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed JSON value that is not a top-level array (an object) hits
    parse_extension_combos's own not-a-list branch, distinct from the per-combo one."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["presets", "create", "greet", "--base-tool", "echo", "--description", "d", "--extensions", "{}"],
    )
    assert result.exit_code != 0
    assert "array of extension combos" in result.output


def test_presets_create_rejects_config_free_object_element(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "presets",
            "create",
            "greet",
            "--base-tool",
            "echo",
            "--description",
            "d",
            "--extensions",
            '[[{"name":"output_schema"}]]',
        ],
    )
    assert result.exit_code != 0
    assert "'config' mapping" in result.output


def test_presets_create_rejects_empty_string_element(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare "" element is not a valid extension name (parse_extension_element str branch)."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["presets", "create", "greet", "--base-tool", "echo", "--description", "d", "--extensions", '[[""]]'],
    )
    assert result.exit_code != 0
    assert "an extension name must be a non-empty string" in result.output


def test_presets_create_rejects_non_string_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """An object element with a non-string 'name' (parse_extension_element dict/name branch)."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "presets",
            "create",
            "greet",
            "--base-tool",
            "echo",
            "--description",
            "d",
            "--extensions",
            '[[{"name":5,"config":{}}]]',
        ],
    )
    assert result.exit_code != 0
    assert "extension element must have" in result.output


def test_presets_create_rejects_object_with_extra_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """An object element with keys beyond name/config (parse_extension_element extra branch)."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "presets",
            "create",
            "greet",
            "--base-tool",
            "echo",
            "--description",
            "d",
            "--extensions",
            '[[{"name":"chain","config":{},"x":1}]]',
        ],
    )
    assert result.exit_code != 0
    assert "unexpected" in result.output


def test_presets_create_rejects_non_element(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-string, non-object element (parse_extension_element fall-through branch)."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["presets", "create", "greet", "--base-tool", "echo", "--description", "d", "--extensions", "[[5]]"],
    )
    assert result.exit_code != 0
    assert "combo element must be an extension" in result.output


def test_presets_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/presets/greet"
        return data_response({"deleted": True})

    result = run_cli(monkeypatch, handler, ["presets", "delete", "greet"])
    assert result.exit_code == 0, result.output


def test_presets_versions_lists_history(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/greet/versions"
        return data_response([{"version": 1, "created_at": "t1"}, {"version": 2, "created_at": "t2"}])

    result = run_cli(monkeypatch, handler, ["presets", "versions", "greet"])
    assert result.exit_code == 0, result.output
    assert "t2" in result.output


def test_presets_get_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/greet/versions/3"
        return data_response({"version": 3, "fixed_kwargs": {"n": 2}})

    result = run_cli(monkeypatch, handler, ["presets", "get-version", "greet", "3"])
    assert result.exit_code == 0, result.output
    assert "3" in result.output


def test_presets_save_version_sends_kwargs_and_extensions(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/greet/versions"
        body = json.loads(request.content)
        assert body["fixed_kwargs"] == {"n": 2}
        assert body["extensions"] == []
        return data_response({"version": 4})

    result = run_cli(
        monkeypatch, handler, ["presets", "save-version", "greet", "--kwargs", '{"n":2}', "--extensions", "[]"]
    )
    assert result.exit_code == 0, result.output


def _capture_preset_create_handler(seen: dict) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets"
        seen.update(json.loads(request.content))
        return data_response({"name": "greet", "persisted": True})

    return handler


def test_presets_create_reads_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "kwargs.json"
    kwargs_file.write_text('{"token": "secret-tok"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_preset_create_handler(seen),
        ["presets", "create", "greet", "--base-tool", "echo", "--description", "d", "--kwargs-file", str(kwargs_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["fixed_kwargs"] == {"token": "secret-tok"}


def test_presets_create_reads_kwargs_file_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_preset_create_handler(seen),
        ["presets", "create", "greet", "--base-tool", "echo", "--description", "d", "--kwargs-file", "-"],
        stdin='{"token": "secret-tok"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["fixed_kwargs"] == {"token": "secret-tok"}


def test_presets_create_rejects_both_kwargs_and_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "kwargs.json"
    kwargs_file.write_text('{"a": 1}')
    result = run_cli(
        monkeypatch,
        _capture_preset_create_handler({}),
        [
            "presets",
            "create",
            "greet",
            "--base-tool",
            "echo",
            "--description",
            "d",
            "--kwargs",
            '{"a":1}',
            "--kwargs-file",
            str(kwargs_file),
        ],
    )
    assert result.exit_code != 0
    assert "--kwargs-file" in result.output


def test_presets_create_rejects_invalid_kwargs_file_json(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "kwargs.json"
    kwargs_file.write_text("not json")
    result = run_cli(
        monkeypatch,
        _capture_preset_create_handler({}),
        ["presets", "create", "greet", "--base-tool", "echo", "--description", "d", "--kwargs-file", str(kwargs_file)],
    )
    assert result.exit_code != 0
    assert "valid JSON" in result.output


def test_presets_save_version_reads_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "kwargs.json"
    kwargs_file.write_text('{"token": "secret-tok"}')

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/greet/versions"
        assert json.loads(request.content)["fixed_kwargs"] == {"token": "secret-tok"}
        return data_response({"version": 6})

    result = run_cli(monkeypatch, handler, ["presets", "save-version", "greet", "--kwargs-file", str(kwargs_file)])
    assert result.exit_code == 0, result.output


def test_presets_validate_reads_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "kwargs.json"
    kwargs_file.write_text('{"token": "secret-tok"}')

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/validate"
        assert json.loads(request.content)["fixed_kwargs"] == {"token": "secret-tok"}
        return data_response({"ok": True})

    result = run_cli(
        monkeypatch, handler, ["presets", "validate", "greet", "--base-tool", "echo", "--kwargs-file", str(kwargs_file)]
    )
    assert result.exit_code == 0, result.output


def test_presets_save_version_sends_description(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/greet/versions"
        # ``--description`` sets the version's description; omitted fields carry forward.
        assert json.loads(request.content) == {"description": "updated"}
        return data_response({"version": 5})

    result = run_cli(monkeypatch, handler, ["presets", "save-version", "greet", "--description", "updated"])
    assert result.exit_code == 0, result.output


def test_presets_save_version_requires_at_least_one_field(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["presets", "save-version", "greet"])
    assert result.exit_code != 0
    assert "at least one" in result.output


def test_presets_rollback_posts_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/presets/greet/rollback"
        assert json.loads(request.content) == {"version": 2}
        return data_response({"active_version": 2})

    result = run_cli(monkeypatch, handler, ["presets", "rollback", "greet", "2"])
    assert result.exit_code == 0, result.output


def test_presets_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/old/rename"
        assert json.loads(request.content) == {"new_name": "new"}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["presets", "rename", "old", "new"]).exit_code == 0


def test_presets_referees(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/p/referees"
        return data_response({"referees": []})

    assert run_cli(monkeypatch, handler, ["presets", "referees", "p"]).exit_code == 0


def test_presets_validate_builds_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/validate"
        body = json.loads(request.content)
        assert body["name"] == "greet"
        assert body["base_tool"] == "echo"
        assert body["fixed_kwargs"] == {"prefix": "hi"}
        return data_response({"verdict": "create"})

    result = run_cli(
        monkeypatch,
        handler,
        ["presets", "validate", "greet", "--base-tool", "echo", "--kwargs", '{"prefix":"hi"}'],
    )
    assert result.exit_code == 0, result.output


def test_presets_set_version_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/presets/p/versions/2/tags"
        assert json.loads(request.content) == {"tags": ["stable"]}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["presets", "set-version-tags", "p", "2", "stable"]).exit_code == 0
