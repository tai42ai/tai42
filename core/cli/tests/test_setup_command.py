"""``tai setup`` exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from tai42_cli.commands import setup as setup_cmd

from .remote_harness import data_response, error_response, run_cli, visible

_RESULT = {
    "owner_user_id": "usr-owner",
    "key_user_id": "usr-owner-key",
    "api_key": "sk-owner",
    "key_fingerprint": "fp-1",
    "login_attached": False,
    "invite_token": None,
    "login_path": None,
}


def _methods(needs_setup: bool, setup_login: dict | None) -> dict:
    return {"methods": [], "needs_setup": needs_setup, "setup_login": setup_login}


def _server(needs_setup: bool, setup_login: dict | None, *, seen: dict, result: dict | None = None):
    """A handler answering both the methods probe and the setup POST, recording the POST body."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Neither call may carry a credential — the caller has no key yet.
        assert "x-api-key" not in request.headers
        assert "authorization" not in request.headers
        if request.url.path == "/api/login/methods":
            assert request.method == "GET"
            return data_response(_methods(needs_setup, setup_login))
        assert request.url.path == "/api/setup"
        assert request.method == "POST"
        seen["posted"] = True
        seen["body"] = json.loads(request.content)
        return data_response(result if result is not None else _RESULT)

    return handler


def test_setup_already_initialized_does_not_post(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=False, setup_login=None, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner"],
    )
    assert result.exit_code == 1, result.output
    assert "already initialized" in result.output
    assert "posted" not in seen


def test_setup_keys_only_sends_minimal_body(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login=None, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner"],
    )
    assert result.exit_code == 0, result.output
    assert seen["body"] == {"setup_token": "tok", "owner_display_name": "Owner", "key_description": "owner key"}
    assert "sk-owner" in result.output


def test_setup_password_login_sends_password_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["password"]}, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner", "--password", "hunter2", "--email", "o@example.com"],
    )
    assert result.exit_code == 0, result.output
    assert seen["body"]["login"] == {"kind": "password", "email": "o@example.com", "password": "hunter2"}


def test_setup_invite_login_sends_invite_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["password", "invite"]}, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner", "--invite", "--email", "o@example.com"],
    )
    assert result.exit_code == 0, result.output
    assert seen["body"]["login"] == {"kind": "invite", "email": "o@example.com"}


def test_setup_invite_announces_the_completion_line(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = {**_RESULT, "login_attached": False, "invite_token": "inv-xyz", "login_path": "/login"}
    outcome = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["invite"]}, seen=seen, result=result),
        ["setup", "--token", "tok", "--display-name", "Owner", "--invite", "--email", "o@example.com"],
    )
    assert outcome.exit_code == 0, outcome.output
    assert "invite token inv-xyz" in outcome.output
    assert "/login" in outcome.output


def test_setup_invite_without_email_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["invite"]}, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner", "--invite"],
    )
    assert result.exit_code != 0
    assert "--email is required" in result.output
    assert "posted" not in seen


def test_setup_invite_unsupported_kind_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["password"]}, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner", "--invite", "--email", "o@example.com"],
    )
    assert result.exit_code != 0
    assert "does not accept an invite" in result.output
    assert "posted" not in seen


def test_setup_password_when_no_login_provider_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login=None, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner", "--password", "pw", "--email", "o@example.com"],
    )
    assert result.exit_code != 0
    assert "no accounts provider that can attach a login" in visible(result.output)
    assert "posted" not in seen


def test_setup_non_tty_without_a_login_choice_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["password", "invite"]}, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner"],
    )
    assert result.exit_code != 0
    assert "choose --password/--password-file, --invite, or --no-login" in result.output
    assert "posted" not in seen


def test_setup_no_login_flag_is_keys_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["password"]}, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner", "--no-login"],
    )
    assert result.exit_code == 0, result.output
    assert "login" not in seen["body"]


def test_setup_token_dash_reads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login=None, seen=seen),
        ["setup", "--token", "-", "--display-name", "Owner"],
        stdin="stdin-tok\n",
    )
    assert result.exit_code == 0, result.output
    assert seen["body"]["setup_token"] == "stdin-tok"


def test_setup_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_SETUP_TOKEN", "env-tok")
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login=None, seen=seen),
        ["setup", "--display-name", "Owner"],
    )
    assert result.exit_code == 0, result.output
    assert seen["body"]["setup_token"] == "env-tok"


def test_setup_absent_token_non_tty_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_SETUP_TOKEN", raising=False)
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login=None, seen=seen),
        ["setup", "--display-name", "Owner"],
    )
    assert result.exit_code != 0
    assert "pass --token" in result.output


def test_setup_password_unsupported_kind_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["invite"]}, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner", "--password", "pw", "--email", "o@example.com"],
    )
    assert result.exit_code != 0
    assert "does not accept a password" in visible(result.output)
    assert "posted" not in seen


def test_setup_reads_password_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["password"]}, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner", "--password-file", "-", "--email", "o@example.com"],
        stdin="file-pw\n",
    )
    assert result.exit_code == 0, result.output
    assert seen["body"]["login"] == {"kind": "password", "email": "o@example.com", "password": "file-pw"}


def test_setup_rejects_token_and_password_both_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["password"]}, seen=seen),
        [
            "setup",
            "--token",
            "-",
            "--display-name",
            "Owner",
            "--password-file",
            "-",
            "--email",
            "o@example.com",
        ],
        stdin="x\n",
    )
    assert result.exit_code != 0
    assert "stdin" in result.output


def test_setup_server_403_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login/methods":
            return data_response(_methods(True, None))
        return error_response("Forbidden", 403)

    result = run_cli(monkeypatch, handler, ["setup", "--token", "bad", "--display-name", "Owner"])
    assert result.exit_code != 0
    assert "Forbidden" in result.output


def test_setup_server_409_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login/methods":
            return data_response(_methods(True, None))
        return error_response("Already initialized", 409)

    result = run_cli(monkeypatch, handler, ["setup", "--token", "tok", "--display-name", "Owner"])
    assert result.exit_code != 0
    assert "Already initialized" in result.output


def test_setup_token_prompt_on_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_cmd, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(setup_cmd.typer, "prompt", lambda *a, **k: "prompted-tok")
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login=None, seen=seen),
        ["setup", "--display-name", "Owner"],
    )
    assert result.exit_code == 0, result.output
    assert seen["body"]["setup_token"] == "prompted-tok"


def test_setup_interactive_login_confirm_yes_sets_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_cmd, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(setup_cmd.typer, "confirm", lambda *a, **k: True)
    prompts = iter(["o@example.com", "typed-pw"])
    monkeypatch.setattr(setup_cmd.typer, "prompt", lambda *a, **k: next(prompts))
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["password"]}, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner"],
    )
    assert result.exit_code == 0, result.output
    assert seen["body"]["login"] == {"kind": "password", "email": "o@example.com", "password": "typed-pw"}


def test_setup_interactive_login_confirm_no_is_keys_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_cmd, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(setup_cmd.typer, "confirm", lambda *a, **k: False)
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["password"]}, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner"],
    )
    assert result.exit_code == 0, result.output
    assert "login" not in seen["body"]


def test_setup_email_without_a_login_choice_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["password"]}, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner", "--email", "o@example.com"],
    )
    assert result.exit_code != 0
    assert "--email requires" in result.output
    assert "posted" not in seen


def test_setup_no_login_with_password_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["password"]}, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner", "--no-login", "--password", "pw", "--email", "o@e.com"],
    )
    assert result.exit_code != 0
    assert "--no-login cannot be combined" in result.output


def test_setup_password_and_invite_together_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login={"kinds": ["password", "invite"]}, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner", "--password", "pw", "--invite", "--email", "o@e.com"],
    )
    assert result.exit_code != 0
    assert "choose only one" in result.output


def test_setup_json_emits_raw_result(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _server(needs_setup=True, setup_login=None, seen=seen),
        ["setup", "--token", "tok", "--display-name", "Owner"],
        json_output=True,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["api_key"] == "sk-owner"


# --- recover path ---------------------------------------------------------

_RECOVER_RESULT = {
    "owner_user_id": "owner",
    "key_user_id": "owner-key",
    "api_key": "sk-owner-key",
    "key_fingerprint": "fp-1",
}


def _no_http(request: httpx.Request) -> httpx.Response:
    """A server that fails the test if any HTTP request is made — the recover path makes none."""
    raise AssertionError(f"unexpected HTTP request to {request.url.path}")


def _register_fake_recovery(monkeypatch: pytest.MonkeyPatch, seen: dict) -> None:
    def handler(setup_token: str, *, key_user: str | None, key_description: str, manifest_path: str | None) -> dict:
        seen["called"] = {
            "setup_token": setup_token,
            "key_user": key_user,
            "key_description": key_description,
            "manifest_path": manifest_path,
        }
        return _RECOVER_RESULT

    monkeypatch.setattr(setup_cmd, "_recovery", handler)


def test_register_recovery_installs_the_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_cmd, "_recovery", None)

    def handler(setup_token: str, *, key_user: str | None, key_description: str, manifest_path: str | None) -> dict:
        return {}

    setup_cmd.register_recovery(handler)
    assert setup_cmd._recovery is handler


def test_recover_unregistered_reports_the_missing_server_package(monkeypatch: pytest.MonkeyPatch) -> None:
    # No server package registered the handler: --recover is a usage error, and no HTTP is made.
    monkeypatch.setattr(setup_cmd, "_recovery", None)
    result = run_cli(monkeypatch, _no_http, ["setup", "--recover", "--token", "tok"])
    assert result.exit_code != 0
    assert "needs the server package" in visible(result.output)


def test_recover_registered_calls_handler_with_resolved_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    _register_fake_recovery(monkeypatch, seen)
    result = run_cli(
        monkeypatch,
        _no_http,
        [
            "setup",
            "--recover",
            "--token",
            "tok",
            "--key-user",
            "owner-key",
            "--key-description",
            "restored owner key",
            "--manifest-path",
            "/deploy/manifest.yaml",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen["called"] == {
        "setup_token": "tok",
        "key_user": "owner-key",
        "key_description": "restored owner key",
        "manifest_path": "/deploy/manifest.yaml",
    }
    assert "sk-owner-key" in result.output
    assert "Owner key shown once" in result.output


def test_recover_rejects_initialize_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    _register_fake_recovery(monkeypatch, seen)
    result = run_cli(monkeypatch, _no_http, ["setup", "--recover", "--token", "tok", "--display-name", "Owner"])
    assert result.exit_code != 0
    assert "--recover re-mints the existing owner's key" in result.output
    assert "called" not in seen


def test_manifest_path_without_recover_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(
        monkeypatch, _no_http, ["setup", "--token", "tok", "--display-name", "Owner", "--manifest-path", "/m.yaml"]
    )
    assert result.exit_code != 0
    assert "--manifest-path applies to --recover only" in result.output


def test_display_name_required_without_recover(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, _no_http, ["setup", "--token", "tok"])
    assert result.exit_code != 0
    assert "--display-name is required" in result.output


def test_recover_token_dash_reads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    _register_fake_recovery(monkeypatch, seen)
    result = run_cli(
        monkeypatch,
        _no_http,
        ["setup", "--recover", "--token", "-", "--manifest-path", "/m.yaml"],
        stdin="stdin-tok\n",
    )
    assert result.exit_code == 0, result.output
    assert seen["called"]["setup_token"] == "stdin-tok"


def test_recover_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_SETUP_TOKEN", "env-tok")
    seen: dict = {}
    _register_fake_recovery(monkeypatch, seen)
    result = run_cli(monkeypatch, _no_http, ["setup", "--recover", "--manifest-path", "/m.yaml"])
    assert result.exit_code == 0, result.output
    assert seen["called"]["setup_token"] == "env-tok"
