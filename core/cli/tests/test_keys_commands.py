"""``tai keys`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_keys_create_returns_raw_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/api-keys"
        body = json.loads(request.content)
        assert body["user_id"] == "alice"
        assert body["scopes"] == ["read"]
        return data_response("sk-secret")

    result = run_cli(
        monkeypatch, handler, ["keys", "create", "--user", "alice", "--description", "ci", "--scope", "read"]
    )
    assert result.exit_code == 0, result.output
    assert "sk-secret" in result.output


def test_keys_edit_requires_a_field(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["keys", "edit", "alice"])
    assert result.exit_code != 0


def test_keys_edit_sets_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/auth/api-keys/alice"
        assert json.loads(request.content) == {"condition": {"content": '.method == "GET"'}}
        return data_response({"user_id": "alice"})

    result = run_cli(
        monkeypatch, handler, ["keys", "edit", "alice", "--condition", '{"content": ".method == \\"GET\\""}']
    )
    assert result.exit_code == 0, result.output


def test_keys_edit_clear_condition_sends_null(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/auth/api-keys/alice"
        assert json.loads(request.content) == {"condition": None}
        return data_response({"user_id": "alice"})

    result = run_cli(monkeypatch, handler, ["keys", "edit", "alice", "--clear-condition"])
    assert result.exit_code == 0, result.output


def test_keys_edit_clear_condition_conflicts_with_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "edit", "alice", "--clear-condition", "--condition", '{"content": "true"}'],
    )
    assert result.exit_code != 0


def test_keys_claim_link_posts_body_and_prints_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/auth/claim-links"
        assert request.headers["x-api-key"] == "test-key"
        # The --ttl flag maps to the wire's ``ttl_seconds`` field.
        assert json.loads(request.content) == {"api_key": "sk-secret", "ttl_seconds": 300}
        return data_response(
            {"claim_path": "/login#claim=clm-xyz", "token": "clm-xyz", "expires_at": "2026-07-16T00:00:00+00:00"}
        )

    result = run_cli(monkeypatch, handler, ["keys", "claim-link", "sk-secret", "--ttl", "300"])
    assert result.exit_code == 0, result.output
    assert "/login#claim=clm-xyz" in result.output


def test_keys_claim_link_reads_key_from_hidden_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the positional key omitted the command reads it from a HIDDEN prompt so the
    # secret never lands in shell history; the prompted key rides the POST body.
    from tai42_cli.commands import keys as keys_module

    prompt_call: dict = {}

    def fake_prompt(text: str, **kwargs: object) -> str:
        prompt_call["text"] = text
        prompt_call["kwargs"] = kwargs
        return "sk-prompted"

    monkeypatch.setattr(keys_module.typer, "prompt", fake_prompt)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/auth/claim-links"
        assert json.loads(request.content) == {"api_key": "sk-prompted"}
        return data_response(
            {"claim_path": "/login#claim=clm-xyz", "token": "clm-xyz", "expires_at": "2026-07-16T00:00:00+00:00"}
        )

    result = run_cli(monkeypatch, handler, ["keys", "claim-link"])
    assert result.exit_code == 0, result.output
    assert "/login#claim=clm-xyz" in result.output
    # The key was read at a HIDDEN prompt (hide_input=True) and never echoed anywhere.
    assert prompt_call["kwargs"].get("hide_input") is True
    assert "sk-prompted" not in result.output


def test_keys_claim_link_omits_ttl_when_flag_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without --ttl the body carries only ``api_key`` — ``ttl_seconds`` is ABSENT, not null.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/auth/claim-links"
        assert json.loads(request.content) == {"api_key": "sk-x"}
        return data_response(
            {"claim_path": "/login#claim=clm-xyz", "token": "clm-xyz", "expires_at": "2026-07-16T00:00:00+00:00"}
        )

    result = run_cli(monkeypatch, handler, ["keys", "claim-link", "sk-x"])
    assert result.exit_code == 0, result.output
    assert "/login#claim=clm-xyz" in result.output


def test_keys_bootstrap_posts_first_admin_credential_free(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/keys/bootstrap"
        # The mint runs credential-free — the caller has no key yet — so no auth header
        # or api-key header rides the request.
        assert request.headers.get("authorization") is None
        assert request.headers.get("x-api-key") is None
        body = json.loads(request.content)
        assert body == {"user_id": "alice", "description": "root key", "bootstrap_token": "tok-from-stdin"}
        return data_response({"token": "sk-secret", "user_id": "alice"})

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "bootstrap", "--user", "alice", "--description", "root key", "--token", "-"],
        stdin="tok-from-stdin\n",
    )
    assert result.exit_code == 0, result.output
    assert "sk-secret" in result.output


def test_keys_list_renders_identity_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/auth/tokens-payload"
        return data_response([{"user_id": "alice", "description": "ci", "scopes": ["read"]}])

    result = run_cli(monkeypatch, handler, ["keys", "list"])
    assert result.exit_code == 0, result.output
    assert "alice" in result.output
    assert "read" in result.output


def test_keys_list_renders_the_orphaned_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/tokens-payload"
        return data_response(
            [
                {"user_id": "alice", "description": "ci", "scopes": ["read"], "orphaned": False},
                {"user_id": "ghost", "description": "", "scopes": ["*"], "orphaned": True},
            ]
        )

    result = run_cli(monkeypatch, handler, ["keys", "list"])
    assert result.exit_code == 0, result.output
    # The orphaned column is rendered in the table (header + the flagged row's value).
    assert "orphaned" in result.output
    assert "ghost" in result.output
    assert "true" in result.output


def test_keys_create_includes_all_optional_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["user_id"] == "bob"
        assert body["condition"] == {"content": '.method == "GET"', "kwargs": {"role": "admin"}}
        assert body["policy_data"] == {"team": "ops"}
        return data_response("sk-secret")

    result = run_cli(
        monkeypatch,
        handler,
        [
            "keys",
            "create",
            "--user",
            "bob",
            "--description",
            "ci",
            "--condition",
            '{"content": ".method == \\"GET\\"", "kwargs": {"role": "admin"}}',
            "--policy-data",
            '{"team":"ops"}',
        ],
    )
    assert result.exit_code == 0, result.output
    assert "sk-secret" in result.output


def test_keys_create_rejects_non_object_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response("sk")

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "create", "--user", "bob", "--description", "ci", "--condition", "[1,2]"],
    )
    assert result.exit_code != 0
    assert "JSON object" in result.output


def test_keys_edit_sends_only_provided_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/auth/api-keys/alice"
        assert json.loads(request.content) == {"description": "new", "scopes": ["read", "write"]}
        return data_response({"user_id": "alice", "updated": True})

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "edit", "alice", "--description", "new", "--scope", "read", "--scope", "write"],
    )
    assert result.exit_code == 0, result.output


def test_keys_edit_writes_every_optional_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/api-keys/alice"
        assert json.loads(request.content) == {
            "condition": {"content": ".ok", "kwargs": {"role": "admin"}},
            "policy_data": {"team": "ops"},
        }
        return data_response({"updated": True})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "keys",
            "edit",
            "alice",
            "--condition",
            '{"content": ".ok", "kwargs": {"role": "admin"}}',
            "--policy-data",
            '{"team":"ops"}',
        ],
    )
    assert result.exit_code == 0, result.output


def test_keys_validate_condition_by_stored_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"condition": {"id": "cond1"}}
        return data_response({"valid": True})

    result = run_cli(monkeypatch, handler, ["keys", "validate-condition", "--condition", '{"id": "cond1"}'])
    assert result.exit_code == 0, result.output


def test_keys_delete_revokes(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/auth/api-keys/alice"
        return data_response({"deleted": True})

    result = run_cli(monkeypatch, handler, ["keys", "delete", "alice"])
    assert result.exit_code == 0, result.output


def test_keys_validate_condition_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/auth/validate-condition"
        body = json.loads(request.content)
        assert body["condition"] == {"content": ".ok", "kwargs": {"n": 1}}
        assert body["sample_context"] == {"method": "GET"}
        return data_response({"valid": True})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "keys",
            "validate-condition",
            "--condition",
            '{"content": ".ok", "kwargs": {"n": 1}}',
            "--sample-context",
            '{"method":"GET"}',
        ],
    )
    assert result.exit_code == 0, result.output
    assert "valid" in result.output


def test_keys_policy_versions_renders_history(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/api-keys/alice/policy/versions"
        return data_response([{"version": 2, "is_current": True, "created_at": "t"}])

    result = run_cli(monkeypatch, handler, ["keys", "policy-versions", "alice"])
    assert result.exit_code == 0, result.output
    assert "2" in result.output


def test_keys_policy_rollback_posts_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/auth/api-keys/alice/policy/rollback"
        assert json.loads(request.content) == {"version": 3}
        return data_response({"version": 3})

    result = run_cli(monkeypatch, handler, ["keys", "policy-rollback", "alice", "3"])
    assert result.exit_code == 0, result.output


def test_keys_scopes_maps_flags_to_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/auth/api-keys/alice/scopes"
        assert json.loads(request.content) == {"add": ["write"], "remove": ["read"]}
        return data_response({"user_id": "alice", "updated": True, "scopes": ["write"]})

    result = run_cli(monkeypatch, handler, ["keys", "scopes", "alice", "--add", "write", "--remove", "read"])
    assert result.exit_code == 0, result.output


def test_keys_scopes_requires_at_least_one_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["keys", "scopes", "alice"])
    assert result.exit_code != 0
    assert "at least one" in result.output


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


def test_keys_create_reads_condition_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cond_file = tmp_path / "cond.json"
    cond_file.write_text('{"content": ".ok", "kwargs": {"scope": "read"}}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_key_create(seen),
        ["keys", "create", "--user", "alice", "--description", "d", "--condition-file", str(cond_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["condition"] == {"content": ".ok", "kwargs": {"scope": "read"}}


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


def test_keys_create_rejects_both_condition_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cond_file = tmp_path / "cond.json"
    cond_file.write_text('{"content": ".ok"}')
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
            "--condition",
            '{"content": ".ok"}',
            "--condition-file",
            str(cond_file),
        ],
    )
    assert result.exit_code != 0
    assert "--condition-file" in result.output


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
            "--condition-file",
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
        ["keys", "edit", "alice", "--policy-data-file", "-", "--condition-file", "-"],
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


def test_keys_edit_reads_condition_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return data_response({"user_id": "alice"})

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "edit", "alice", "--condition-file", "-"],
        stdin='{"content": ".ok", "kwargs": {"scope": "read"}}',
    )
    assert result.exit_code == 0, result.output
    assert seen["condition"] == {"content": ".ok", "kwargs": {"scope": "read"}}


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


def test_validate_condition_reads_condition_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cond_file = tmp_path / "cond.json"
    cond_file.write_text('{"content": ".x", "kwargs": {"scope": "read"}}')
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/validate-condition"
        seen.update(json.loads(request.content))
        return data_response({"ok": True})

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "validate-condition", "--condition-file", str(cond_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["condition"] == {"content": ".x", "kwargs": {"scope": "read"}}


def test_validate_condition_reads_condition_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return data_response({"ok": True})

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "validate-condition", "--condition-file", "-"],
        stdin='{"content": ".x", "kwargs": {"scope": "read"}}',
    )
    assert result.exit_code == 0, result.output
    assert seen["condition"] == {"content": ".x", "kwargs": {"scope": "read"}}


def test_validate_condition_rejects_both_condition_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cond_file = tmp_path / "cond.json"
    cond_file.write_text('{"content": ".x"}')

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(
        monkeypatch,
        handler,
        ["keys", "validate-condition", "--condition", '{"content": ".x"}', "--condition-file", str(cond_file)],
    )
    assert result.exit_code != 0
    assert "--condition-file" in result.output
