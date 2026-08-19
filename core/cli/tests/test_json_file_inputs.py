"""Coverage for the ``--X-file`` (and stdin ``-``) companions that let a secret
JSON body be kept off the command line.

Every wired command accepts its JSON object from a file or from stdin (``-``)
instead of an inline flag, so a value never lands on argv (where ``ps`` and shell
history would expose it). These tests drive each site against the fake ``/api/*``
server: the file source shapes the same request body as the inline flag, ``-``
reads it from stdin, and giving both the inline and file source is a usage error.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from .remote_harness import data_response, run_cli

# -- hooks register ----------------------------------------------------------


def _capture_hook_register(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/hooks"
        seen.update(json.loads(request.content))
        return data_response({"name": "h1", "registered": True})

    return handler


def test_hooks_register_reads_params_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    params_file = tmp_path / "params.json"
    params_file.write_text('{"name":"h1","topic":"gh","tool":"notify","execution_key":"svc"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch, _capture_hook_register(seen), ["hooks", "register", "--params-file", str(params_file)]
    )
    assert result.exit_code == 0, result.output
    assert seen["execution_key"] == "svc"


def test_hooks_register_reads_params_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_hook_register(seen),
        ["hooks", "register", "--params-file", "-"],
        stdin='{"name":"h1","topic":"gh","tool":"notify","execution_key":"svc"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["topic"] == "gh"


def test_hooks_register_rejects_both_params_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    params_file = tmp_path / "params.json"
    params_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _capture_hook_register({}),
        ["hooks", "register", "--params", "{}", "--params-file", str(params_file)],
    )
    assert result.exit_code != 0
    assert "--params-file" in result.output


def test_hooks_register_rejects_neither_params_nor_file(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, _capture_hook_register({}), ["hooks", "register"])
    assert result.exit_code != 0
    assert "--params" in result.output


# -- hooks create-trigger-link -----------------------------------------------


def _capture_trigger_link(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/hooks/trigger-links"
        seen.update(json.loads(request.content))
        return data_response({"name": "lnk", "topic": "events", "trigger_path": "/x/tok", "expires_at": None})

    return handler


def test_create_trigger_link_reads_params_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    params_file = tmp_path / "p.json"
    params_file.write_text('{"p":"hi"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_trigger_link(seen),
        [
            "hooks",
            "create-trigger-link",
            "events",
            "--execution-key",
            "svc",
            "--permanent",
            "--params-file",
            str(params_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["tool_kwargs"] == {"p": "hi"}


def test_create_trigger_link_reads_params_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_trigger_link(seen),
        ["hooks", "create-trigger-link", "events", "--execution-key", "svc", "--permanent", "--params-file", "-"],
        stdin='{"p":"hi"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["tool_kwargs"] == {"p": "hi"}


def test_create_trigger_link_rejects_both_params_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    params_file = tmp_path / "p.json"
    params_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _capture_trigger_link({}),
        [
            "hooks",
            "create-trigger-link",
            "events",
            "--execution-key",
            "svc",
            "--permanent",
            "--params",
            "{}",
            "--params-file",
            str(params_file),
        ],
    )
    assert result.exit_code != 0
    assert "--params-file" in result.output


# -- connectors connect ------------------------------------------------------


def _capture_connect(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/connectors/connections/start"
        seen.update(json.loads(request.content))
        return data_response({"authorize_url": "https://x"})

    return handler


def test_connect_reads_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config_file = tmp_path / "c.json"
    config_file.write_text('{"api_key":"secret"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_connect(seen),
        ["connectors", "connect", "prov", "--alias", "work", "--sub-service", "svc", "--config-file", str(config_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["config_values"] == {"api_key": "secret"}


def test_connect_reads_config_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_connect(seen),
        ["connectors", "connect", "prov", "--alias", "work", "--sub-service", "svc", "--config-file", "-"],
        stdin='{"api_key":"secret"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["config_values"] == {"api_key": "secret"}


def test_connect_rejects_both_config_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config_file = tmp_path / "c.json"
    config_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _capture_connect({}),
        [
            "connectors",
            "connect",
            "prov",
            "--alias",
            "work",
            "--sub-service",
            "svc",
            "--config",
            "{}",
            "--config-file",
            str(config_file),
        ],
    )
    assert result.exit_code != 0
    assert "--config-file" in result.output


# -- keys create / edit / validate-condition ---------------------------------


def _capture_key_create(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/api-keys"
        seen.update(json.loads(request.content))
        return data_response({"user_id": "alice", "api_key": "sk-x"})

    return handler


def test_keys_create_reads_policy_data_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    policy_file = tmp_path / "pd.json"
    policy_file.write_text('{"tier":"gold"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_key_create(seen),
        ["keys", "create", "--user", "alice", "--description", "d", "--policy-data-file", str(policy_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["policy_data"] == {"tier": "gold"}


def test_keys_create_reads_policy_data_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_key_create(seen),
        ["keys", "create", "--user", "alice", "--description", "d", "--policy-data-file", "-"],
        stdin='{"tier":"gold"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["policy_data"] == {"tier": "gold"}


def test_keys_create_reads_condition_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ck_file = tmp_path / "ck.json"
    ck_file.write_text('{"scope":"read"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_key_create(seen),
        ["keys", "create", "--user", "alice", "--description", "d", "--condition-kwargs-file", str(ck_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["condition_kwargs"] == {"scope": "read"}


def test_keys_create_rejects_both_policy_data_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    policy_file = tmp_path / "pd.json"
    policy_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _capture_key_create({}),
        [
            "keys",
            "create",
            "--user",
            "alice",
            "--description",
            "d",
            "--policy-data",
            "{}",
            "--policy-data-file",
            str(policy_file),
        ],
    )
    assert result.exit_code != 0
    assert "--policy-data-file" in result.output


def test_keys_create_rejects_both_condition_kwargs_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ck_file = tmp_path / "ck.json"
    ck_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _capture_key_create({}),
        [
            "keys",
            "create",
            "--user",
            "alice",
            "--description",
            "d",
            "--condition-kwargs",
            "{}",
            "--condition-kwargs-file",
            str(ck_file),
        ],
    )
    assert result.exit_code != 0
    assert "--condition-kwargs-file" in result.output


def test_keys_create_rejects_two_stdin_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(
        monkeypatch,
        _capture_key_create({}),
        [
            "keys",
            "create",
            "--user",
            "alice",
            "--description",
            "d",
            "--policy-data-file",
            "-",
            "--condition-kwargs-file",
            "-",
        ],
        stdin='{"tier":"gold"}',
    )
    assert result.exit_code != 0
    assert "stdin" in result.output
    assert "must be valid JSON" not in result.output


def test_keys_edit_rejects_two_stdin_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "edit", "alice", "--policy-data-file", "-", "--condition-kwargs-file", "-"],
        stdin='{"tier":"gold"}',
    )
    assert result.exit_code != 0
    assert "stdin" in result.output
    assert "must be valid JSON" not in result.output


def test_keys_edit_reads_policy_data_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    policy_file = tmp_path / "pd.json"
    policy_file.write_text('{"tier":"gold"}')
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/auth/api-keys/alice"
        seen.update(json.loads(request.content))
        return data_response({"user_id": "alice"})

    result = run_cli(monkeypatch, handler, ["keys", "edit", "alice", "--policy-data-file", str(policy_file)])
    assert result.exit_code == 0, result.output
    assert seen["policy_data"] == {"tier": "gold"}


def test_keys_edit_reads_condition_kwargs_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return data_response({"user_id": "alice"})

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "edit", "alice", "--condition-kwargs-file", "-"],
        stdin='{"scope":"read"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["condition_kwargs"] == {"scope": "read"}


def test_keys_edit_rejects_both_policy_data_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    policy_file = tmp_path / "pd.json"
    policy_file.write_text("{}")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "edit", "alice", "--policy-data", "{}", "--policy-data-file", str(policy_file)],
    )
    assert result.exit_code != 0
    assert "--policy-data-file" in result.output


def test_validate_condition_reads_condition_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ck_file = tmp_path / "ck.json"
    ck_file.write_text('{"scope":"read"}')
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/validate-condition"
        seen.update(json.loads(request.content))
        return data_response({"ok": True})

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "validate-condition", "--condition", ".x", "--condition-kwargs-file", str(ck_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["condition_kwargs"] == {"scope": "read"}


def test_validate_condition_reads_condition_kwargs_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return data_response({"ok": True})

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "validate-condition", "--condition", ".x", "--condition-kwargs-file", "-"],
        stdin='{"scope":"read"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["condition_kwargs"] == {"scope": "read"}


def test_validate_condition_rejects_both_condition_kwargs_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ck_file = tmp_path / "ck.json"
    ck_file.write_text("{}")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "validate-condition", "--condition-kwargs", "{}", "--condition-kwargs-file", str(ck_file)],
    )
    assert result.exit_code != 0
    assert "--condition-kwargs-file" in result.output


# -- tools run / runs submit -------------------------------------------------


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


# -- schedules add -----------------------------------------------------------


def _capture_schedule_add(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/schedules"
        seen.update(json.loads(request.content))
        return data_response({"name": "s1"})

    return handler


def test_schedules_add_reads_tool_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "tk.json"
    kwargs_file.write_text('{"token":"secret"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch, _capture_schedule_add(seen), ["schedules", "add", "t", "--tool-kwargs-file", str(kwargs_file)]
    )
    assert result.exit_code == 0, result.output
    assert seen["tool_kwargs"] == {"token": "secret"}


def test_schedules_add_reads_tool_kwargs_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_schedule_add(seen),
        ["schedules", "add", "t", "--tool-kwargs-file", "-"],
        stdin='{"token":"secret"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["tool_kwargs"] == {"token": "secret"}


def test_schedules_add_tool_kw_overrides_tool_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "tk.json"
    kwargs_file.write_text('{"a":1,"token":"secret"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_schedule_add(seen),
        ["schedules", "add", "t", "--tool-kwargs-file", str(kwargs_file), "--tool-kw", "a=2"],
    )
    assert result.exit_code == 0, result.output
    assert seen["tool_kwargs"] == {"a": 2, "token": "secret"}


def test_schedules_add_rejects_both_tool_kwargs_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "tk.json"
    kwargs_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _capture_schedule_add({}),
        ["schedules", "add", "t", "--tool-kwargs", "{}", "--tool-kwargs-file", str(kwargs_file)],
    )
    assert result.exit_code != 0
    assert "--tool-kwargs-file" in result.output


# -- agents run / authored-run -----------------------------------------------


def _agent_sse(request: httpx.Request) -> httpx.Response:
    body = 'data: {"type":"done"}\n\n'
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def test_agents_run_reads_input_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    input_file = tmp_path / "in.json"
    input_file.write_text('{"query":"weather"}')
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/researcher/runs"
        seen.update(json.loads(request.content))
        return _agent_sse(request)

    result = run_cli(monkeypatch, handler, ["agents", "run", "researcher", "--input-file", str(input_file)])
    assert result.exit_code == 0, result.output
    assert seen == {"query": "weather"}


def test_agents_run_reads_input_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _agent_sse(request)

    result = run_cli(
        monkeypatch,
        handler,
        ["agents", "run", "researcher", "--input-file", "-"],
        stdin='{"query":"weather"}',
    )
    assert result.exit_code == 0, result.output
    assert seen == {"query": "weather"}


def test_agents_run_rejects_both_input_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    input_file = tmp_path / "in.json"
    input_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _agent_sse,
        ["agents", "run", "researcher", "--input", "{}", "--input-file", str(input_file)],
    )
    assert result.exit_code != 0
    assert "--input-file" in result.output


def test_agents_run_rejects_neither_input_nor_file(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, _agent_sse, ["agents", "run", "researcher"])
    assert result.exit_code != 0
    assert "--input" in result.output


def test_agents_authored_run_reads_input_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    input_file = tmp_path / "in.json"
    input_file.write_text('{"query":"weather"}')
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/authored/my_agent/runs"
        seen.update(json.loads(request.content))
        return _agent_sse(request)

    result = run_cli(monkeypatch, handler, ["agents", "authored-run", "my_agent", "--input-file", str(input_file)])
    assert result.exit_code == 0, result.output
    assert seen == {"query": "weather"}


def test_agents_authored_run_reads_input_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/authored/my_agent/runs"
        seen.update(json.loads(request.content))
        return _agent_sse(request)

    result = run_cli(
        monkeypatch,
        handler,
        ["agents", "authored-run", "my_agent", "--input-file", "-"],
        stdin='{"query":"weather"}',
    )
    assert result.exit_code == 0, result.output
    assert seen == {"query": "weather"}


def test_agents_authored_run_rejects_both_input_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    input_file = tmp_path / "in.json"
    input_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _agent_sse,
        ["agents", "authored-run", "my_agent", "--input", "{}", "--input-file", str(input_file)],
    )
    assert result.exit_code != 0
    assert "--input-file" in result.output


def test_agents_authored_run_rejects_neither_input_nor_file(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, _agent_sse, ["agents", "authored-run", "my_agent"])
    assert result.exit_code != 0
    assert "--input" in result.output


# -- templates render --------------------------------------------------------


def _capture_render_template(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/render-template"
        seen.update(json.loads(request.content))
        return data_response({"rendered": "hi"})

    return handler


def test_templates_render_reads_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "k.json"
    kwargs_file.write_text('{"name":"secret"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_render_template(seen),
        ["templates", "render", "--template-id", "prompts/greeting.md", "--kwargs-file", str(kwargs_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["kwargs"] == {"name": "secret"}


def test_templates_render_reads_kwargs_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_render_template(seen),
        ["templates", "render", "--template-id", "prompts/greeting.md", "--kwargs-file", "-"],
        stdin='{"name":"secret"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["kwargs"] == {"name": "secret"}


def test_templates_render_rejects_both_kwargs_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "k.json"
    kwargs_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _capture_render_template({}),
        [
            "templates",
            "render",
            "--template-id",
            "prompts/greeting.md",
            "--kwargs",
            "{}",
            "--kwargs-file",
            str(kwargs_file),
        ],
    )
    assert result.exit_code != 0
    assert "--kwargs-file" in result.output


# -- resources get (render) --------------------------------------------------


def _capture_resource_render(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/resources/get"
        seen.update(json.loads(request.content))
        return data_response({"content": "hi"})

    return handler


def test_resources_render_reads_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "k.json"
    kwargs_file.write_text('{"name":"secret"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_resource_render(seen),
        ["resources", "get", "prompts/greeting.md", "--kwargs-file", str(kwargs_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["template_kwargs"] == {"name": "secret"}


def test_resources_render_reads_kwargs_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_resource_render(seen),
        ["resources", "get", "prompts/greeting.md", "--kwargs-file", "-"],
        stdin='{"name":"secret"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["template_kwargs"] == {"name": "secret"}


def test_resources_render_rejects_both_kwargs_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "k.json"
    kwargs_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _capture_resource_render({}),
        ["resources", "get", "prompts/greeting.md", "--kwargs", "{}", "--kwargs-file", str(kwargs_file)],
    )
    assert result.exit_code != 0
    assert "--kwargs-file" in result.output


# -- storage upload ----------------------------------------------------------


def _capture_upload(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/storage/resources"
        seen.update(json.loads(request.content))
        return data_response({"id": "r"})

    return handler


def test_storage_upload_reads_file_bytes(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    content = b"\x00secret-bytes\xff"
    src = tmp_path / "blob.bin"
    src.write_bytes(content)
    seen: dict = {}
    result = run_cli(monkeypatch, _capture_upload(seen), ["storage", "upload", "r", "--file", str(src)])
    assert result.exit_code == 0, result.output
    assert base64.b64decode(seen["content_base64"]) == content


def test_storage_upload_reads_bytes_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"\x00secret-bytes\xff"
    seen: dict = {}
    result = run_cli(monkeypatch, _capture_upload(seen), ["storage", "upload", "r", "--file", "-"], stdin=content)
    assert result.exit_code == 0, result.output
    assert base64.b64decode(seen["content_base64"]) == content


def test_storage_upload_rejects_text_and_file_together(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    src = tmp_path / "blob.bin"
    src.write_bytes(b"x")
    result = run_cli(
        monkeypatch,
        _capture_upload({}),
        ["storage", "upload", "r", "--text", "hi", "--file", str(src)],
    )
    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_storage_upload_rejects_no_source(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, _capture_upload({}), ["storage", "upload", "r"])
    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_storage_upload_missing_file_raises(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    result = run_cli(
        monkeypatch,
        _capture_upload({}),
        ["storage", "upload", "r", "--file", str(tmp_path / "nope.bin")],
    )
    assert result.exit_code != 0
    assert "--file" in result.output


# -- auth claim (stdin) ------------------------------------------------------


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
