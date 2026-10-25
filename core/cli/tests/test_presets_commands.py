"""``tai presets`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import Handler, data_response, error_response, run_cli, visible


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
    """A bare "" element is not a valid extension name."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["presets", "create", "greet", "--base-tool", "echo", "--description", "d", "--extensions", '[[""]]'],
    )
    assert result.exit_code != 0
    assert "non-empty extension name" in visible(result.output)


def test_presets_create_rejects_non_string_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """An object element with a non-string 'name'."""

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
    """An object element with keys beyond name/config."""

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
    """A non-string, non-object element."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["presets", "create", "greet", "--base-tool", "echo", "--description", "d", "--extensions", "[[5]]"],
    )
    assert result.exit_code != 0
    assert "non-empty extension name" in visible(result.output)


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


_HOLDERS_PREVIEW = {
    "action": "rename",
    "holders": [
        {"kind": "preset", "name": "f", "locator": {}, "summary": "preset 'f'"},
        {"kind": "schedule", "name": "nightly", "locator": {"id": "nightly"}, "summary": "schedule 'nightly'"},
    ],
}


def test_presets_list_prints_an_unreadable_stored_row(monkeypatch: pytest.MonkeyPatch) -> None:
    message = "stored preset 'bad': fixed_kwargs.tool_names must be a list of tool names"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": message, "code": "stored-preset-unreadable", "preset": "bad"})

    result = run_cli(monkeypatch, handler, ["presets", "list"])
    assert result.exit_code == 1
    assert visible(result.output) == f"Error (HTTP 500): {message}"


def test_presets_rename_prints_every_holder_of_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    message = "preset 'p' cannot be renamed — held by:\n- preset 'f'\nupdate those references first"

    def handler(request: httpx.Request) -> httpx.Response:
        return error_response(message, 409)

    result = run_cli(monkeypatch, handler, ["presets", "rename", "p", "q"])
    assert result.exit_code == 1
    assert result.output.splitlines() == [
        "Error (HTTP 409): preset 'p' cannot be renamed — held by:",
        "- preset 'f'",
        "update those references first",
    ]


def test_presets_referees_prints_one_row_per_holder(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/p/referees"
        assert request.url.params["action"] == "rename"
        return data_response(_HOLDERS_PREVIEW)

    result = run_cli(monkeypatch, handler, ["presets", "referees", "p", "--action", "rename"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "kind      name     locator            summary           ",
        "--------  -------  -----------------  ------------------",
        "preset    f        {}                 preset 'f'        ",
        'schedule  nightly  {"id": "nightly"}  schedule \'nightly\'',
    ]


def test_presets_referees_json_prints_the_raw_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["action"] == "delete"
        return data_response({"action": "delete", "holders": []})

    result = run_cli(monkeypatch, handler, ["presets", "referees", "p", "--action", "delete"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"action": "delete", "holders": []}


def test_presets_referees_requires_the_action(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request is sent without --action")

    result = run_cli(monkeypatch, handler, ["presets", "referees", "p"])
    assert result.exit_code == 2
    assert "Missing option '--action'" in visible(result.output)


def test_presets_referees_refuses_an_unknown_action(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request is sent for an unknown action")

    result = run_cli(monkeypatch, handler, ["presets", "referees", "p", "--action", "detach"])
    assert result.exit_code == 2
    assert "'detach' is not one of 'rename', 'delete'" in visible(result.output)


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


_MARKER = "!ENV ${SECRET_TOKEN}"


def test_presets_get_renders_the_reference_marker_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    # A preset whose ``fixed_kwargs`` carries an ``!ENV`` reference renders the marker
    # as the API returned it — the passthrough never resolves it to a value.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/greet"
        return data_response({"name": "greet", "base_tool": "echo", "fixed_kwargs": {"token": _MARKER}})

    result = run_cli(monkeypatch, handler, ["presets", "get", "greet"], json_output=True)
    assert result.exit_code == 0, result.output
    assert _MARKER in result.output


def test_presets_versions_render_the_reference_marker_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/greet/versions"
        return data_response([{"version": 1, "body": {"base_tool": "echo", "fixed_kwargs": {"token": _MARKER}}}])

    result = run_cli(monkeypatch, handler, ["presets", "versions", "greet"], json_output=True)
    assert result.exit_code == 0, result.output
    assert _MARKER in result.output


def test_presets_kwargs_help_names_the_reference_convention(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - help never calls the API
        return data_response({})

    for command in ("create", "save-version", "validate"):
        result = run_cli(monkeypatch, handler, ["presets", command, "--help"])
        assert result.exit_code == 0, result.output
        text = visible(result.output)
        assert "!ENV" in text, text
        assert "secret reference" in text, text


# -- body fields: schemas, state binding, input and output maps ---------------

_COMMANDS = {
    "create": (["presets", "create", "greet", "--base-tool", "echo", "--description", "d"], "/api/presets"),
    "save-version": (["presets", "save-version", "greet"], "/api/presets/greet/versions"),
    "validate": (["presets", "validate", "greet"], "/api/presets/validate"),
}
_CLEARING = ["save-version", "validate"]

_SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}}
_SCHEMA_REFERENCE = {"id": "schema_template", "kwargs": {"n": 1}}
_STATE_BINDING = {
    "states": [
        {
            "template": "counter",
            "subject_expr": {"content": ".subject", "kwargs": {}},
            "inject": [{"program": {"id": "inject_program", "kwargs": {}}}],
        }
    ]
}


def _capture(seen: dict, path: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == path
        seen.update(json.loads(request.content))
        return data_response({"name": "greet"})

    return handler


def _run(monkeypatch: pytest.MonkeyPatch, command: str, extra: list[str], *, stdin: str | None = None) -> dict:
    args, path = _COMMANDS[command]
    seen: dict = {}
    result = run_cli(monkeypatch, _capture(seen, path), [*args, *extra], stdin=stdin)
    assert result.exit_code == 0, result.output
    return seen


def _refused(monkeypatch: pytest.MonkeyPatch, command: str, extra: list[str], *, stdin: str | None = None) -> str:
    args, _path = _COMMANDS[command]

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("a refused command sent a request")

    result = run_cli(monkeypatch, handler, [*args, *extra], stdin=stdin)
    assert result.exit_code != 0
    return visible(result.output)


@pytest.mark.parametrize("command", list(_COMMANDS))
@pytest.mark.parametrize("field", ["output_schema", "input_schema"])
@pytest.mark.parametrize("value", [_SCHEMA, _SCHEMA_REFERENCE], ids=["schema", "template-reference"])
def test_a_schema_option_puts_the_object_on_the_wire_as_written(
    monkeypatch: pytest.MonkeyPatch, command: str, field: str, value: dict
) -> None:
    option = "--" + field.replace("_", "-")
    seen = _run(monkeypatch, command, [option, json.dumps(value)])
    assert seen[field] == value


@pytest.mark.parametrize("command", list(_COMMANDS))
@pytest.mark.parametrize("field", ["output_schema", "input_schema"])
def test_a_schema_file_option_reads_a_file_and_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path, command: str, field: str
) -> None:
    option = "--" + field.replace("_", "-") + "-file"
    source = tmp_path / "schema.json"
    source.write_text(json.dumps(_SCHEMA))
    assert _run(monkeypatch, command, [option, str(source)])[field] == _SCHEMA
    assert _run(monkeypatch, command, [option, "-"], stdin=json.dumps(_SCHEMA_REFERENCE))[field] == _SCHEMA_REFERENCE


@pytest.mark.parametrize("command", list(_COMMANDS))
def test_the_state_binding_option_carries_the_whole_object_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path, command: str
) -> None:
    assert (
        _run(monkeypatch, command, ["--state-binding", json.dumps(_STATE_BINDING)])["state_binding"] == _STATE_BINDING
    )
    source = tmp_path / "binding.json"
    source.write_text(json.dumps(_STATE_BINDING))
    assert _run(monkeypatch, command, ["--state-binding-file", str(source)])["state_binding"] == _STATE_BINDING
    assert (
        _run(monkeypatch, command, ["--state-binding-file", "-"], stdin=json.dumps(_STATE_BINDING))["state_binding"]
        == _STATE_BINDING
    )


@pytest.mark.parametrize("command", list(_COMMANDS))
@pytest.mark.parametrize("field", ["input_map", "output_map"])
def test_a_map_is_sent_as_text_from_inline_file_and_stdin_and_as_an_object_from_its_template(
    monkeypatch: pytest.MonkeyPatch, tmp_path, command: str, field: str
) -> None:
    option = "--" + field.replace("_", "-")
    program = '{payload: (.text + ":" + .n)}\n'
    assert _run(monkeypatch, command, [option, program])[field] == program
    source = tmp_path / "program.jq"
    source.write_text(program)
    assert _run(monkeypatch, command, [f"{option}-file", str(source)])[field] == program
    assert _run(monkeypatch, command, [f"{option}-file", "-"], stdin=program)[field] == program
    reference = {"id": "stored_program", "kwargs": {"k": "v"}}
    assert _run(monkeypatch, command, [f"{option}-template", json.dumps(reference)])[field] == reference


@pytest.mark.parametrize("command", list(_COMMANDS))
def test_an_omitted_field_is_not_sent(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    seen = _run(monkeypatch, command, ["--description", "d2"] if command != "create" else [])
    for field in ("output_schema", "input_schema", "state_binding", "input_map", "output_map"):
        assert field not in seen


@pytest.mark.parametrize("command", list(_COMMANDS))
@pytest.mark.parametrize("option", ["--output-schema", "--input-schema", "--state-binding"])
def test_both_of_a_pair_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, tmp_path, command: str, option: str) -> None:
    source = tmp_path / "value.json"
    source.write_text("{}")
    output = _refused(monkeypatch, command, [option, "{}", f"{option}-file", str(source)])
    assert f"give only one of {option} or {option}-file" in output


@pytest.mark.parametrize("command", list(_COMMANDS))
@pytest.mark.parametrize("option", ["--input-map", "--output-map"])
@pytest.mark.parametrize("pair", [("", "-file"), ("", "-template"), ("-file", "-template")])
def test_two_of_a_maps_three_forms_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path, command: str, option: str, pair: tuple[str, str]
) -> None:
    source = tmp_path / "program.jq"
    source.write_text(".")
    values = {"": ".", "-file": str(source), "-template": '{"id": "p"}'}
    extra = [arg for suffix in pair for arg in (f"{option}{suffix}", values[suffix])]
    output = _refused(monkeypatch, command, extra)
    assert f"give only one of {option}, {option}-file or {option}-template" in output


@pytest.mark.parametrize("command", list(_COMMANDS))
@pytest.mark.parametrize(
    "option",
    ["--output-schema", "--input-schema", "--state-binding", "--input-map-template", "--output-map-template"],
)
def test_a_non_object_json_is_refused(monkeypatch: pytest.MonkeyPatch, command: str, option: str) -> None:
    assert "must be a JSON object" in _refused(monkeypatch, command, [option, "[1]"])


def test_two_options_reading_stdin_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _refused(
        monkeypatch, "create", ["--input-schema-file", "-", "--input-map-file", "-"], stdin=json.dumps(_SCHEMA)
    )
    assert "only one option may read from stdin ('-')" in output


@pytest.mark.parametrize("command", _CLEARING)
@pytest.mark.parametrize("field", ["output_schema", "input_schema", "state_binding", "input_map", "output_map"])
def test_a_clear_option_sends_null(monkeypatch: pytest.MonkeyPatch, command: str, field: str) -> None:
    seen = _run(monkeypatch, command, ["--clear-" + field.replace("_", "-")])
    assert field in seen
    assert seen[field] is None


@pytest.mark.parametrize("command", _CLEARING)
@pytest.mark.parametrize(
    ("set_args", "named"),
    [
        (["--output-schema", "{}"], "--output-schema"),
        (["--output-schema-file", "-"], "--output-schema"),
        (["--input-schema", "{}"], "--input-schema"),
        (["--state-binding", "{}"], "--state-binding"),
        (["--input-map", "."], "--input-map"),
        (["--input-map-template", '{"id": "p"}'], "--input-map"),
        (["--output-map-file", "-"], "--output-map"),
    ],
)
def test_a_set_option_with_its_clear_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, command: str, set_args: list[str], named: str
) -> None:
    clear = "--clear-" + named.removeprefix("--")
    output = _refused(monkeypatch, command, [*set_args, clear], stdin="{}")
    assert f"pass either {named} or {clear}, not both" in output


@pytest.mark.parametrize(
    "clear",
    [
        "--clear-output-schema",
        "--clear-input-schema",
        "--clear-state-binding",
        "--clear-input-map",
        "--clear-output-map",
    ],
)
def test_create_has_no_clear(monkeypatch: pytest.MonkeyPatch, clear: str) -> None:
    assert "No such option" in _refused(monkeypatch, "create", [clear])


def test_the_save_version_emptiness_refusal_names_every_option(monkeypatch: pytest.MonkeyPatch) -> None:
    output = _refused(monkeypatch, "save-version", [])
    assert (
        "provide at least one of --kwargs, --description, --extensions, --output-schema, --input-schema, "
        "--state-binding, --input-map, --output-map (or their -file / -template forms), or a --clear-* option"
    ) in output


def test_presets_create_prints_the_servers_stored_schema_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    message = "stored id 'schemas/missing' at output_schema is not a stored template"

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["output_schema"] == {"id": "schemas/missing"}
        return httpx.Response(400, json={"error": message})

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
            "--output-schema",
            '{"id": "schemas/missing"}',
        ],
    )
    assert result.exit_code == 1
    assert result.stderr.strip() == f"Error (HTTP 400): {message}"
