"""``tai tools`` (and ``tai extensions``) command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli


def _parse_tool_call(request: httpx.Request) -> tuple[str, dict[str, object]]:
    """Extract ``(tool_name, arguments)`` from the posted body, asserting the
    ``{tool_name, arguments}`` shape the run-tool door requires: a non-empty string
    ``tool_name`` and an object ``arguments`` defaulting to ``{}``. A CLI body that
    drifts from that shape fails here; the live server's acceptance of the same body
    is covered by the e2e ``tools run`` leg."""
    body = json.loads(request.content)
    assert isinstance(body, dict), body
    name = body.get("tool_name", "")
    assert isinstance(name, str), body
    assert name, body
    arguments = body.get("arguments", {})
    assert isinstance(arguments, dict), body
    return name, arguments


def test_tools_list_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/tools"
        assert request.headers["x-api-key"] == "test-key"
        return data_response(["alpha", "beta"])

    result = run_cli(monkeypatch, handler, ["tools", "list"])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "beta" in result.output


def test_tools_list_json_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response(["alpha", "beta"])

    result = run_cli(monkeypatch, handler, ["tools", "list"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == ["alpha", "beta"]


def test_tools_run_posts_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/run-tool"
        # Validate through the route's own parser: the body must be the
        # ``{tool_name, arguments}`` shape ``read_tool_call`` enforces, or a live
        # server 400s. Asserting the literal dict alone let the two drift apart.
        assert _parse_tool_call(request) == ("add", {"a": 1, "b": 2})
        return data_response({"sum": 3})

    result = run_cli(monkeypatch, handler, ["tools", "run", "add", "--kw", "a=1", "--kw", "b=2"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"sum": 3}


@pytest.mark.parametrize(
    ("door_tool", "arguments"),
    [
        ("send_conversation_message", {"route_name": "chat-line", "external_user_id": "u-1", "text": "hi"}),
        ("send_conversation_event", {"route_name": "chat-line", "thread_id": "t-1", "event": {"event_id": "e-1"}}),
    ],
)
def test_tools_run_dispatches_builtin_door_tool(
    monkeypatch: pytest.MonkeyPatch, door_tool: str, arguments: dict[str, object]
) -> None:
    """The in-process conversation-door builtins run through the plain ``tools run`` command — no
    door-specific subcommand. The tool name rides ``tool_name`` and its args ride ``arguments``,
    the same ``{tool_name, arguments}`` body every tool uses; the server resolves the builtin."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/run-tool"
        assert _parse_tool_call(request) == (door_tool, arguments)
        return data_response({"message_id": "m-1", "thread_id": "t-1"})

    result = run_cli(
        monkeypatch, handler, ["tools", "run", door_tool, "--kwargs", json.dumps(arguments)], json_output=True
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"message_id": "m-1", "thread_id": "t-1"}


def test_tools_run_posts_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``--subject-*`` flags ride the run-tool body as the ``subject`` object the edge
    validates into a ``StateSubject`` (``target_kind`` is ``tool``)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/run-tool"
        body = json.loads(request.content)
        assert body["subject"] == {
            "target_kind": "tool",
            "target_name": "acct-42",
            "kind": "thread",
            "key": "t-1",
        }
        return data_response({"ok": 1})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "tools",
            "run",
            "add",
            "--subject-kind",
            "thread",
            "--subject-key",
            "t-1",
            "--subject-target",
            "acct-42",
        ],
        json_output=True,
    )
    assert result.exit_code == 0, result.output


def test_tools_run_omits_subject_when_no_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no ``--subject-*`` flag the body carries no ``subject`` key, so the edge reads ``None``."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "subject" not in body
        return data_response({"ok": 1})

    result = run_cli(monkeypatch, handler, ["tools", "run", "add"], json_output=True)
    assert result.exit_code == 0, result.output


def test_tools_run_partial_subject_flags_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The three subject flags are all-or-nothing: giving one alone is a usage error, before any call."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made when the subject flags are incomplete")

    result = run_cli(monkeypatch, handler, ["tools", "run", "add", "--subject-kind", "thread"])
    assert result.exit_code != 0
    assert "subject" in result.output.lower()


def test_tools_runs_submit_posts_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """The submit door carries the same ``subject`` object the sync door does."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tool-runs"
        body = json.loads(request.content)
        assert body["subject"] == {
            "target_kind": "tool",
            "target_name": "acct-42",
            "kind": "thread",
            "key": "t-1",
        }
        return data_response({"run_id": "r1"})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "tools",
            "runs",
            "submit",
            "slow",
            "--subject-kind",
            "thread",
            "--subject-key",
            "t-1",
            "--subject-target",
            "acct-42",
        ],
        json_output=True,
    )
    assert result.exit_code == 0, result.output


def test_tools_schema_not_found_surfaces_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("Tool 'nope' not registered", 404)

    result = run_cli(monkeypatch, handler, ["tools", "schema", "nope"])
    assert result.exit_code != 0
    assert "Tool 'nope' not registered" in result.output


def test_tools_apply_sends_full_combo_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tools/my_tool/extensions"
        assert json.loads(request.content) == {"combos": [["chain", "batch"], ["chain"]]}
        return data_response({"ok": True})

    result = run_cli(
        monkeypatch, handler, ["tools", "apply", "my_tool", "--combo", '["chain","batch"]', "--combo", '["chain"]']
    )
    assert result.exit_code == 0, result.output


def test_tools_apply_carries_element_config_losslessly(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = {"type": "object"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"combos": [[{"name": "output_schema", "config": {"schema": schema}}]]}
        return data_response({"ok": True})

    result = run_cli(
        monkeypatch,
        handler,
        ["tools", "apply", "my_tool", "--combo", '[{"name":"output_schema","config":{"schema":{"type":"object"}}}]'],
    )
    assert result.exit_code == 0, result.output


def test_tools_apply_mixes_bare_and_configured_elements(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "combos": [["chain", {"name": "output_schema", "config": {"schema": {"type": "string"}}}]]
        }
        return data_response({"ok": True})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "tools",
            "apply",
            "my_tool",
            "--combo",
            '["chain",{"name":"output_schema","config":{"schema":{"type":"string"}}}]',
        ],
    )
    assert result.exit_code == 0, result.output


def test_tools_apply_no_combo_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"combos": []}
        return data_response({"ok": True})

    result = run_cli(monkeypatch, handler, ["tools", "apply", "my_tool"])
    assert result.exit_code == 0, result.output


def test_tools_apply_rejects_malformed_combo_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["tools", "apply", "my_tool", "--combo", "not json"])
    assert result.exit_code != 0
    assert "valid JSON" in result.output


def test_tools_apply_rejects_config_free_object_element(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["tools", "apply", "my_tool", "--combo", '[{"name":"output_schema"}]'])
    assert result.exit_code != 0
    assert "'config' mapping" in result.output


def test_tools_apply_rejects_empty_combo(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["tools", "apply", "my_tool", "--combo", "[]"])
    assert result.exit_code != 0
    assert "non-empty" in result.output


def test_tools_apply_rejects_empty_string_element(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare "" is not a valid extension name (parse_extension_element str branch)."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["tools", "apply", "my_tool", "--combo", '[""]'])
    assert result.exit_code != 0
    assert "an extension name must be a non-empty string" in result.output


def test_tools_apply_rejects_object_without_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """An object element missing 'name' (parse_extension_element dict/name branch)."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["tools", "apply", "my_tool", "--combo", '[{"config":{}}]'])
    assert result.exit_code != 0
    # (substring stays on one wrapped panel line regardless of the option prefix width)
    assert "extension element must have" in result.output


def test_tools_apply_rejects_object_with_extra_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """An object element with keys beyond name/config (parse_extension_element extra branch)."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch, handler, ["tools", "apply", "my_tool", "--combo", '[{"name":"chain","config":{},"extra":1}]']
    )
    assert result.exit_code != 0
    assert "unexpected keys" in result.output


def test_tools_apply_rejects_non_element(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-string, non-object element (parse_extension_element fall-through branch)."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["tools", "apply", "my_tool", "--combo", "[5]"])
    assert result.exit_code != 0
    assert "must be an extension name or a" in result.output


def test_tools_extensions_add_posts_add_side(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tools/my_tool/extensions/combos"
        assert json.loads(request.content) == {"add": [["chain", "batch"], ["chain"]], "remove": []}
        return data_response({"combos": [["chain", "batch"], ["chain"]]})

    result = run_cli(
        monkeypatch,
        handler,
        ["tools", "extensions-add", "my_tool", "--combo", '["chain","batch"]', "--combo", '["chain"]'],
    )
    assert result.exit_code == 0, result.output


def test_tools_extensions_remove_posts_remove_side(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tools/my_tool/extensions/combos"
        assert json.loads(request.content) == {"add": [], "remove": [["chain"]]}
        return data_response({"combos": []})

    result = run_cli(monkeypatch, handler, ["tools", "extensions-remove", "my_tool", "--combo", '["chain"]'])
    assert result.exit_code == 0, result.output


def test_tools_extensions_add_carries_element_config_losslessly(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "add": [[{"name": "output_schema", "config": {"schema": {"type": "object"}}}]],
            "remove": [],
        }
        return data_response({"combos": []})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "tools",
            "extensions-add",
            "my_tool",
            "--combo",
            '[{"name":"output_schema","config":{"schema":{"type":"object"}}}]',
        ],
    )
    assert result.exit_code == 0, result.output


def test_tools_extensions_add_rejects_non_array_combo(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["tools", "extensions-add", "my_tool", "--combo", '"chain"'])
    assert result.exit_code != 0
    assert "non-empty JSON array" in result.output


def test_tools_extensions_remove_surfaces_absent_combo_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("combo [\"chain\"] is not attached to tool 'my_tool'", 404)

    result = run_cli(monkeypatch, handler, ["tools", "extensions-remove", "my_tool", "--combo", '["chain"]'])
    assert result.exit_code != 0
    assert "not attached" in result.output


def test_tools_runs_submit_posts_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tool-runs"
        # Same ``{tool_name, arguments}`` parser seam the submit door uses.
        assert _parse_tool_call(request) == ("slow", {"n": 100})
        return data_response({"run_id": "r1"})

    result = run_cli(monkeypatch, handler, ["tools", "runs", "submit", "slow", "--kw", "n=100"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"run_id": "r1"}


def test_tools_runs_list_passes_tool_name(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tool-runs"
        assert request.url.params.get("tool_name") == "slow"
        return data_response([{"run_id": "r1", "tool_name": "slow", "status": "running", "started_at": "t"}])

    result = run_cli(monkeypatch, handler, ["tools", "runs", "list", "slow"])
    assert result.exit_code == 0, result.output
    assert "r1" in result.output


def test_extensions_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/extensions"
        return data_response([{"name": "chain", "kind": "wrapper"}])

    result = run_cli(monkeypatch, handler, ["extensions", "list"])
    assert result.exit_code == 0, result.output
    assert "chain" in result.output


def test_unauthenticated_surfaces_clear_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("missing key", 401)

    result = run_cli(monkeypatch, handler, ["tools", "list"])
    assert result.exit_code != 0
    assert "not authenticated" in result.output


def test_tools_tags_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tools/tags"
        return data_response([{"name": "add", "tags": ["math"]}])

    result = run_cli(monkeypatch, handler, ["tools", "tags"])
    assert result.exit_code == 0, result.output
    assert "add" in result.output
    assert "math" in result.output


def test_tools_schema_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tools/add/schema"
        return data_response({"input": {"a": "int"}})

    result = run_cli(monkeypatch, handler, ["tools", "schema", "add"])
    assert result.exit_code == 0, result.output


def test_tools_schemas_all(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tools-schema"
        return data_response({"add": {"input": {}}})

    result = run_cli(monkeypatch, handler, ["tools", "schemas"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"add": {"input": {}}}


def test_tools_extensions_shows_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tools/add/extensions"
        return data_response({"applied": [["chain"]], "catalog": ["chain", "batch"]})

    result = run_cli(monkeypatch, handler, ["tools", "extensions", "add"])
    assert result.exit_code == 0, result.output


def test_tools_runs_submit_posts_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tool-runs"
        assert json.loads(request.content) == {"tool_name": "slow", "arguments": {"n": 100}}
        return data_response({"run_id": "r1", "status": "running"})

    result = run_cli(monkeypatch, handler, ["tools", "runs", "submit", "slow", "--kw", "n=100"])
    assert result.exit_code == 0, result.output
    assert "r1" in result.output


def test_tools_runs_get(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tool-runs/r1"
        return data_response({"run_id": "r1", "status": "succeeded"})

    result = run_cli(monkeypatch, handler, ["tools", "runs", "get", "r1"])
    assert result.exit_code == 0, result.output
    assert "succeeded" in result.output


def _capture_run_tool(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/run-tool"
        seen.update(json.loads(request.content))
        return data_response({"result": 1})

    return handler


def test_tools_run_reads_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "k.json"
    kwargs_file.write_text('{"token":"secret"}')
    seen: dict = {}
    result = run_cli(monkeypatch, _capture_run_tool(seen), ["tools", "run", "t", "--kwargs-file", str(kwargs_file)])
    assert result.exit_code == 0, result.output
    assert seen["arguments"] == {"token": "secret"}


def test_tools_run_reads_kwargs_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_run_tool(seen),
        ["tools", "run", "t", "--kwargs-file", "-"],
        stdin='{"token":"secret"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["arguments"] == {"token": "secret"}


def test_tools_run_kw_overrides_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "k.json"
    kwargs_file.write_text('{"a":1,"token":"secret"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_run_tool(seen),
        ["tools", "run", "t", "--kwargs-file", str(kwargs_file), "--kw", "a=2"],
    )
    assert result.exit_code == 0, result.output
    assert seen["arguments"] == {"a": 2, "token": "secret"}


def test_tools_run_rejects_both_kwargs_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "k.json"
    kwargs_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _capture_run_tool({}),
        ["tools", "run", "t", "--kwargs", "{}", "--kwargs-file", str(kwargs_file)],
    )
    assert result.exit_code != 0
    assert "--kwargs-file" in result.output


def test_tools_runs_submit_reads_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "k.json"
    kwargs_file.write_text('{"token":"secret"}')
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tool-runs"
        seen.update(json.loads(request.content))
        return data_response({"run_id": "r1"})

    result = run_cli(monkeypatch, handler, ["tools", "runs", "submit", "t", "--kwargs-file", str(kwargs_file)])
    assert result.exit_code == 0, result.output
    assert seen["arguments"] == {"token": "secret"}


def test_tools_runs_submit_rejects_both_kwargs_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "k.json"
    kwargs_file.write_text("{}")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["tools", "runs", "submit", "t", "--kwargs", "{}", "--kwargs-file", str(kwargs_file)],
    )
    assert result.exit_code != 0
    assert "--kwargs-file" in result.output
