"""``tai config`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import Handler, data_response, run_cli, visible


def test_config_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/config/env"
        assert json.loads(request.content) == {"LOG_LEVEL": "debug"}
        return data_response({"reloaded": True})

    result = run_cli(monkeypatch, handler, ["config", "env", "set", "LOG_LEVEL=debug"])
    assert result.exit_code == 0, result.output


def test_config_profile_list_renders_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/config/profiles"
        return data_response([{"name": "staging", "description": "Staging band"}])

    result = run_cli(monkeypatch, handler, ["config", "profile", "list"])
    assert result.exit_code == 0, result.output
    assert "staging" in result.output


def test_config_profile_show_reads_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/config/profiles/staging"
        assert not request.content
        return data_response({"description": "d", "env": {"LOG_LEVEL": "debug"}, "secret_keys": []})

    result = run_cli(monkeypatch, handler, ["config", "profile", "show", "staging"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["env"] == {"LOG_LEVEL": "debug"}


def test_config_profile_set_puts_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/config/profiles/staging"
        assert json.loads(request.content) == {
            "description": "Staging band",
            "env": {"LOG_LEVEL": "debug", "API_KEY": "xyz"},
            "secret_keys": ["API_KEY"],
        }
        return data_response({"ok": True, "version": 1})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "config",
            "profile",
            "set",
            "staging",
            "LOG_LEVEL=debug",
            "API_KEY=xyz",
            "--description",
            "Staging band",
            "--secret-key",
            "API_KEY",
        ],
    )
    assert result.exit_code == 0, result.output


def test_config_profile_set_empty_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # A whole-body replace with no source at all is a legitimate empty band —
    # the request carries an empty env, clearing the stored env.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/config/profiles/staging"
        assert json.loads(request.content)["env"] == {}
        return data_response({"ok": True, "version": 1})

    result = run_cli(monkeypatch, handler, ["config", "profile", "set", "staging"])
    assert result.exit_code == 0, result.output


def test_config_profile_set_env_file_empty_errors(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # A given --env-file that yields no assignment is a mistake, not an empty band.
    env_file = tmp_path / "band.env"
    env_file.write_text("# only comments\n\n")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["config", "profile", "set", "staging", "--env-file", str(env_file)])
    assert result.exit_code != 0
    assert "--env-file" in result.output
    assert "no KEY=VALUE assignments" in result.output


def test_config_profile_set_stdin_empty_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    # A given --stdin that yields no assignment is a mistake, not an empty band.
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch, handler, ["config", "profile", "set", "staging", "--stdin"], stdin="# header only\n\n"
    )
    assert result.exit_code != 0
    assert "read from stdin" in result.output


def test_config_profile_set_reads_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / "band.env"
    env_file.write_text("# comment\nLOG_LEVEL=debug\nAPI_KEY=whsec_abc\n")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/config/profiles/staging"
        assert json.loads(request.content)["env"] == {"LOG_LEVEL": "debug", "API_KEY": "whsec_abc"}
        return data_response({"ok": True, "version": 1})

    result = run_cli(monkeypatch, handler, ["config", "profile", "set", "staging", "--env-file", str(env_file)])
    assert result.exit_code == 0, result.output


def test_config_profile_set_reads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/config/profiles/staging"
        assert json.loads(request.content)["env"] == {"DB_URL": "postgres://secret"}
        return data_response({"ok": True, "version": 1})

    result = run_cli(
        monkeypatch,
        handler,
        ["config", "profile", "set", "staging", "--stdin"],
        stdin="# header\nDB_URL=postgres://secret\n",
    )
    assert result.exit_code == 0, result.output


def test_config_profile_set_collision_argv_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / "band.env"
    env_file.write_text("KEY=fromfile\n")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch, handler, ["config", "profile", "set", "staging", "KEY=fromargv", "--env-file", str(env_file)]
    )
    assert result.exit_code != 0
    assert "'KEY'" in result.output


def test_config_profile_set_env_file_bad_line_no_echo(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / "band.env"
    # An empty-key line's value after '=' may be a real secret; the error names the
    # line number only, never the line content.
    env_file.write_text("=whsec_value\n")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["config", "profile", "set", "staging", "--env-file", str(env_file)])
    assert result.exit_code != 0
    assert "line 1" in visible(result.output)
    assert "whsec_value" not in result.output


def test_config_profile_set_rejects_reserved_name(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["config", "profile", "set", "@previous", "X=1"])
    assert result.exit_code != 0
    assert "reserved" in result.output


def test_config_profile_set_rejects_malformed_assignment(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["config", "profile", "set", "staging", "NOTANASSIGNMENT"])
    assert result.exit_code != 0


def test_config_profile_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/config/profiles/staging"
        return data_response({"ok": True})

    result = run_cli(monkeypatch, handler, ["config", "profile", "delete", "staging"])
    assert result.exit_code == 0, result.output


def test_config_profile_diff_posts_no_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/config/profiles/staging/diff"
        assert not request.content
        return data_response({"added": ["A"], "removed": [], "changed": [], "recycle_keys": [], "refused_keys": []})

    result = run_cli(monkeypatch, handler, ["config", "profile", "diff", "staging"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["added"] == ["A"]


def test_config_profile_apply_posts_no_body_prints_report(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/config/profiles/staging/apply"
        assert not request.content
        return data_response(
            {
                "hot": ["LOG_LEVEL"],
                "recycle": [{"origin": "self", "kind": "serve", "status": "self-deferred"}],
                "refused": [],
                "fanout": {},
            }
        )

    result = run_cli(monkeypatch, handler, ["config", "profile", "apply", "staging"], json_output=True)
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["hot"] == ["LOG_LEVEL"]
    assert body["refused"] == []


def test_config_profile_versions_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/config/profiles/staging/versions"
        return data_response([{"version": 1, "tags": [], "created_at": "t", "is_current": True}])

    result = run_cli(monkeypatch, handler, ["config", "profile", "versions", "staging"])
    assert result.exit_code == 0, result.output
    assert "1" in result.output


def test_config_profile_versions_show_one(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/config/profiles/staging/versions/3"
        return data_response({"version": 3, "tags": [], "created_at": "t", "is_current": False, "body": {"env": {}}})

    result = run_cli(
        monkeypatch, handler, ["config", "profile", "versions", "staging", "--version", "3"], json_output=True
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["version"] == 3


def test_config_profile_rollback_posts_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/config/profiles/staging/rollback"
        assert json.loads(request.content) == {"version": 2}
        return data_response({"ok": True, "version": 2})

    result = run_cli(monkeypatch, handler, ["config", "profile", "rollback", "staging", "2"])
    assert result.exit_code == 0, result.output


def test_config_profile_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    # set -> list -> show -> diff -> versions -> rollback each hit their door in turn.
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "PUT" and path == "/api/config/profiles/staging":
            return data_response({"ok": True, "version": 1})
        if method == "GET" and path == "/api/config/profiles":
            return data_response([{"name": "staging", "description": "d"}])
        if method == "GET" and path == "/api/config/profiles/staging":
            return data_response({"description": "d", "env": {"A": "1"}, "secret_keys": []})
        if method == "POST" and path == "/api/config/profiles/staging/diff":
            return data_response({"added": [], "removed": [], "changed": [], "recycle_keys": [], "refused_keys": []})
        if method == "GET" and path == "/api/config/profiles/staging/versions":
            return data_response([{"version": 1, "tags": [], "created_at": "t", "is_current": True}])
        if method == "POST" and path == "/api/config/profiles/staging/rollback":
            return data_response({"ok": True, "version": 1})
        raise AssertionError(f"unexpected request: {method} {path}")

    for args in (
        ["config", "profile", "set", "staging", "A=1"],
        ["config", "profile", "list"],
        ["config", "profile", "show", "staging"],
        ["config", "profile", "diff", "staging"],
        ["config", "profile", "versions", "staging"],
        ["config", "profile", "rollback", "staging", "1"],
    ):
        result = run_cli(monkeypatch, handler, args)
        assert result.exit_code == 0, result.output


def test_config_env_get(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/config/env"
        return data_response({"LOG_LEVEL": "info"})

    result = run_cli(monkeypatch, handler, ["config", "env", "get"])
    assert result.exit_code == 0, result.output
    assert "LOG_LEVEL" in result.output


def test_config_env_set_rejects_bare_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["config", "env", "set", "NOEQUALS"])
    assert result.exit_code != 0
    assert "KEY=VALUE" in result.output


def _capture_env_handler(seen: dict) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/config/env"
        seen.update(json.loads(request.content))
        return data_response({"reloaded": True})

    return handler


def test_config_env_set_reads_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / "secrets.env"
    env_file.write_text("# a comment\n\nWEBHOOK_SECRET=whsec_abc=extra\n  \nTOKEN = tok123\n")
    seen: dict = {}
    result = run_cli(monkeypatch, _capture_env_handler(seen), ["config", "env", "set", "--env-file", str(env_file)])
    assert result.exit_code == 0, result.output
    # Split on the FIRST '=' (value keeps the rest verbatim); key stripped of spaces.
    assert seen == {"WEBHOOK_SECRET": "whsec_abc=extra", "TOKEN": " tok123"}


def test_config_env_set_env_file_no_equals_line_raises(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / "bad.env"
    # A bare token with no '=' may itself be a raw secret; the error names the line
    # number only, never the line content.
    env_file.write_text("GOOD=1\nraw_secret_xyz\n")
    result = run_cli(monkeypatch, _capture_env_handler({}), ["config", "env", "set", "--env-file", str(env_file)])
    assert result.exit_code != 0
    assert "line 2" in visible(result.output)
    assert "raw_secret_xyz" not in result.output


def test_config_env_set_env_file_empty_key_line_raises(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / "bad.env"
    # An empty-key line's value after '=' may be a real secret; the error names the
    # line number only, never the line content.
    env_file.write_text("=whsec_value\n")
    result = run_cli(monkeypatch, _capture_env_handler({}), ["config", "env", "set", "--env-file", str(env_file)])
    assert result.exit_code != 0
    assert "line 1" in visible(result.output)
    assert "whsec_value" not in result.output


def test_config_env_set_env_file_empty_errors(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # A given --env-file that yields no assignment is a mistake, not a no-op merge.
    env_file = tmp_path / "empty.env"
    env_file.write_text("# only comments\n\n")
    result = run_cli(monkeypatch, _capture_env_handler({}), ["config", "env", "set", "--env-file", str(env_file)])
    assert result.exit_code != 0
    assert "--env-file" in result.output
    assert "no KEY=VALUE assignments" in result.output


def test_config_env_set_missing_env_file_fails_at_parse(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    missing = tmp_path / "nope.env"
    result = run_cli(monkeypatch, _capture_env_handler({}), ["config", "env", "set", "--env-file", str(missing)])
    assert result.exit_code != 0
    assert "--env-file" in result.output


def test_config_env_set_reads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_env_handler(seen),
        ["config", "env", "set", "--stdin"],
        stdin="# header\nDB_URL=postgres://secret\n",
    )
    assert result.exit_code == 0, result.output
    assert seen == {"DB_URL": "postgres://secret"}


def test_config_env_set_empty_stdin_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(
        monkeypatch, _capture_env_handler({}), ["config", "env", "set", "--stdin"], stdin="\n# only comment\n"
    )
    assert result.exit_code != 0
    assert "--stdin" in result.output


def test_config_env_set_no_assignment_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, _capture_env_handler({}), ["config", "env", "set"])
    assert result.exit_code != 0
    assert "at least one" in result.output


def test_config_env_set_collision_argv_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / "s.env"
    env_file.write_text("KEY=fromfile\n")
    result = run_cli(
        monkeypatch, _capture_env_handler({}), ["config", "env", "set", "KEY=fromargv", "--env-file", str(env_file)]
    )
    assert result.exit_code != 0
    assert "'KEY'" in result.output


def test_config_env_set_collision_file_and_stdin(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / "s.env"
    env_file.write_text("KEY=fromfile\n")
    result = run_cli(
        monkeypatch,
        _capture_env_handler({}),
        ["config", "env", "set", "--env-file", str(env_file), "--stdin"],
        stdin="KEY=fromstdin\n",
    )
    assert result.exit_code != 0
    assert "'KEY'" in result.output


def test_config_env_set_collision_twice_in_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / "s.env"
    env_file.write_text("KEY=one\nKEY=two\n")
    result = run_cli(monkeypatch, _capture_env_handler({}), ["config", "env", "set", "--env-file", str(env_file)])
    assert result.exit_code != 0
    assert "'KEY'" in result.output


def test_config_env_set_composes_disjoint_sources(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / "s.env"
    env_file.write_text("B=fromfile\n")
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_env_handler(seen),
        ["config", "env", "set", "A=fromargv", "--env-file", str(env_file), "--stdin"],
        stdin="C=fromstdin\n",
    )
    assert result.exit_code == 0, result.output
    assert seen == {"A": "fromargv", "B": "fromfile", "C": "fromstdin"}


def test_config_env_set_does_not_echo_secret_value(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(
        monkeypatch,
        _capture_env_handler({}),
        ["config", "env", "set", "--stdin"],
        stdin="WEBHOOK_SECRET=whsec_topsecret\n",
    )
    assert result.exit_code == 0, result.output
    assert "whsec_topsecret" not in result.output


def test_config_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/config/mode"
        return data_response({"mode": "file"})

    result = run_cli(monkeypatch, handler, ["config", "mode"])
    assert result.exit_code == 0, result.output
    assert "file" in result.output


def test_config_settings_schema_uses_groups_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/config/settings-schema"
        return data_response({"groups": [{"name": "redis", "module": "tai42_skeleton.settings"}]})

    result = run_cli(monkeypatch, handler, ["config", "settings-schema"])
    assert result.exit_code == 0, result.output
    assert "redis" in result.output
