"""``tai hooks`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_hooks_register_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/hooks"
        assert json.loads(request.content) == {"name": "h1", "topic": "gh", "tool": "notify"}
        return data_response({"registered": True, "name": "h1"})

    result = run_cli(
        monkeypatch, handler, ["hooks", "register", "--params", '{"name":"h1","topic":"gh","tool":"notify"}']
    )
    assert result.exit_code == 0, result.output


def test_hooks_verifiers_lists_names(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/hooks/verifiers"
        return data_response(["github_hmac", "shared_secret"])

    result = run_cli(monkeypatch, handler, ["hooks", "verifiers"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == ["github_hmac", "shared_secret"]


def _trigger_link_reply(topic: str = "events") -> dict:
    return {
        "name": "trg-link-deadbeef",
        "trigger_path": "/trigger/SECRET",
        "token": "SECRET",
        "topic": topic,
        "expires_at": None,
    }


def test_hooks_trigger_links_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/hooks/trigger-links"
        return data_response(
            {"items": [{"name": "l1", "topic": "events", "expires_at": None, "token_hash_prefix": "abc"}], "total": 1}
        )

    result = run_cli(monkeypatch, handler, ["hooks", "trigger-links"])
    assert result.exit_code == 0, result.output
    assert "l1" in result.output


def test_hooks_create_trigger_link_timed_composes_absolute_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/hooks/trigger-links"
        assert json.loads(request.content) == {
            "topic": "events",
            "execution_key": "svc-events",
            "ttl_seconds": 3600,
            "require_api_key": False,
        }
        return data_response(_trigger_link_reply())

    result = run_cli(
        monkeypatch,
        handler,
        ["hooks", "create-trigger-link", "events", "--execution-key", "svc-events", "--ttl", "3600"],
        json_output=True,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["url"] == "http://testserver/trigger/SECRET"


def test_hooks_create_trigger_link_permanent_null_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "topic": "events",
            "execution_key": "svc-events",
            "ttl_seconds": None,
            "require_api_key": False,
        }
        return data_response(_trigger_link_reply())

    result = run_cli(
        monkeypatch, handler, ["hooks", "create-trigger-link", "events", "--execution-key", "svc-events", "--permanent"]
    )
    assert result.exit_code == 0, result.output


def test_hooks_create_trigger_link_params_land_in_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tool_kwargs"] == {"example_config_kwargs": {"x": 1}}
        assert body["name"] == "mylink"
        assert body["execution_key"] == "svc-events"
        assert body["require_api_key"] is True
        return data_response(_trigger_link_reply())

    result = run_cli(
        monkeypatch,
        handler,
        [
            "hooks",
            "create-trigger-link",
            "events",
            "--execution-key",
            "svc-events",
            "--permanent",
            "--require-api-key",
            "--name",
            "mylink",
            "--params",
            '{"example_config_kwargs":{"x":1}}',
        ],
    )
    assert result.exit_code == 0, result.output


def test_hooks_create_trigger_link_requires_neither_flag_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response(_trigger_link_reply())

    result = run_cli(monkeypatch, handler, ["hooks", "create-trigger-link", "events", "--execution-key", "svc-events"])
    # Neither flag → a loud usage error (no silent default), never a request.
    assert result.exit_code != 0


def test_hooks_create_trigger_link_both_flags_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response(_trigger_link_reply())

    result = run_cli(
        monkeypatch,
        handler,
        ["hooks", "create-trigger-link", "events", "--execution-key", "svc-events", "--ttl", "60", "--permanent"],
    )
    # Both flags → a loud usage error, never a request.
    assert result.exit_code != 0


def test_hooks_create_trigger_link_trailing_slash_base_no_double_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response(_trigger_link_reply())

    result = run_cli(
        monkeypatch,
        handler,
        [
            "--server",
            "http://testserver/",
            "hooks",
            "create-trigger-link",
            "events",
            "--execution-key",
            "svc-events",
            "--permanent",
        ],
        json_output=True,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["url"] == "http://testserver/trigger/SECRET"


def test_hooks_delete_trigger_link(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/hooks/trigger-links/mylink"
        return data_response({"removed": True, "name": "mylink"})

    result = run_cli(monkeypatch, handler, ["hooks", "delete-trigger-link", "mylink"])
    assert result.exit_code == 0, result.output


def test_hooks_list_filters_by_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/hooks"
        assert request.url.params.get("topic") == "github"
        return data_response({"items": [{"name": "h1", "topic": "github", "tool": "notify"}]})

    result = run_cli(monkeypatch, handler, ["hooks", "list", "--topic", "github"])
    assert result.exit_code == 0, result.output
    assert "h1" in result.output


def test_hooks_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/hooks/h1"
        return data_response({"deleted": True})

    result = run_cli(monkeypatch, handler, ["hooks", "delete", "h1"])
    assert result.exit_code == 0, result.output


def test_hooks_set_verifier_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/hooks/topics/github/verifier"
        assert json.loads(request.content) == {"verifier": "github_hmac", "config": {"secret_env": "GH"}}
        return data_response({"bound": True})

    result = run_cli(
        monkeypatch,
        handler,
        ["hooks", "set-verifier", "github", "--verifier", "github_hmac", "--config", '{"secret_env":"GH"}'],
    )
    assert result.exit_code == 0, result.output


def test_hooks_delete_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/hooks/topics/github/verifier"
        return data_response({"removed": True})

    result = run_cli(monkeypatch, handler, ["hooks", "delete-verifier", "github"])
    assert result.exit_code == 0, result.output


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
