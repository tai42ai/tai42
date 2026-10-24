"""Offline ``manifest validate`` and ``config lint`` — no server required.

Both load a manifest file and run it through the in-repo :class:`Manifest` model;
a valid file is accepted, a broken one is rejected loudly with the model's error and
a non-zero exit. ``config lint`` additionally checks that required registered
settings resolve. No client, database, or Redis is touched.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
import typer
from click.testing import CliRunner
from tai42_cli import app as app_module

_OAUTH_CONNECTOR = {
    "id": "acme",
    "display_name": "Acme",
    "icon_url": "https://example.com/acme.png",
    "kind": "oauth",
    "origin": "system",
    "category": "productivity",
    "oauth": {"authorize": "https://auth.example.com/authorize", "token": "https://auth.example.com/token"},
    "client_id_env": "ACME_CLIENT_ID",
    "client_secret_env": "ACME_CLIENT_SECRET",
    "sub_services": {
        "main": {
            "id": "main",
            "display_name": "Main",
            "scopes": ["read"],
            "mcp_server": {"type": "http", "url": "https://mcp.example.com/mcp"},
        }
    },
}


def _config_lint_in_a_clean_process(*args: str) -> tuple[int, str]:
    """``tai config lint`` in its own interpreter, with this process's environment and directory.

    The lint reads the process-wide settings registry, so it runs where only the groups
    its own command imports are registered, never a group another test imported here.
    Returns the exit code and the combined output.
    """
    result = subprocess.run(
        [sys.executable, "-c", "from tai42_cli.app import app; app()", "config", "lint", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


_VALID_MANIFEST = """
tools:
  - title: toolbox
    module: tai42_skeleton.examples
    include: []
"""

_BROKEN_MANIFEST = """
tools:
  - module: 123
"""


def test_manifest_validate_accepts_valid(tmp_path) -> None:
    path = tmp_path / "manifest.yml"
    path.write_text(_VALID_MANIFEST, encoding="utf-8")

    result = CliRunner().invoke(app_module.app, ["manifest", "validate", str(path)])
    assert result.exit_code == 0, result.output
    assert "is valid" in result.output


def test_manifest_validate_rejects_broken(tmp_path) -> None:
    path = tmp_path / "manifest.yml"
    path.write_text(_BROKEN_MANIFEST, encoding="utf-8")

    result = CliRunner().invoke(app_module.app, ["manifest", "validate", str(path)])
    assert result.exit_code != 0
    assert "invalid manifest" in result.output


def test_manifest_validate_missing_file() -> None:
    result = CliRunner().invoke(app_module.app, ["manifest", "validate", "/no/such/manifest.yml"])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_offline_leaves_have_no_completion_flags() -> None:
    # The offline holders are built with completion disabled, so these leaves do not
    # advertise ``--install-completion``/``--show-completion``.
    for path in (["config", "lint", "--help"], ["manifest", "validate", "--help"]):
        result = CliRunner().invoke(app_module.app, path)
        assert result.exit_code == 0, result.output
        assert "--install-completion" not in result.output
        assert "--show-completion" not in result.output


def test_offline_leaves_have_no_json_flag() -> None:
    # The offline validators emit human diagnostics and an exit code only; they never
    # honor ``--json``, so the flag must not be advertised on their help.
    for path in (["config", "lint", "--help"], ["manifest", "validate", "--help"]):
        result = CliRunner().invoke(app_module.app, path)
        assert result.exit_code == 0, result.output
        assert "--json" not in result.output


def test_config_lint_accepts_valid(tmp_path) -> None:
    path = tmp_path / "manifest.yml"
    path.write_text(_VALID_MANIFEST, encoding="utf-8")

    exit_code, output = _config_lint_in_a_clean_process(str(path))
    assert exit_code == 0, output
    assert "is valid" in output
    assert "required settings resolve" in output


def test_config_lint_rejects_broken_manifest(tmp_path) -> None:
    path = tmp_path / "manifest.yml"
    path.write_text(_BROKEN_MANIFEST, encoding="utf-8")

    result = CliRunner().invoke(app_module.app, ["config", "lint", str(path)])
    assert result.exit_code != 0
    assert "invalid manifest" in result.output


_NESTED_UNKNOWN_KEY_MANIFEST = """
api_tools:
  expose_destrutive: false
"""


def test_config_lint_refuses_an_unknown_nested_key_naming_it(tmp_path) -> None:
    path = tmp_path / "manifest.yml"
    path.write_text(_NESTED_UNKNOWN_KEY_MANIFEST, encoding="utf-8")

    result = CliRunner().invoke(app_module.app, ["config", "lint", str(path)])
    assert result.exit_code != 0
    assert "invalid manifest" in result.output
    assert "api_tools.expose_destrutive" in result.output


def test_manifest_validate_refuses_an_unknown_nested_key_naming_it(tmp_path) -> None:
    path = tmp_path / "manifest.yml"
    path.write_text(_NESTED_UNKNOWN_KEY_MANIFEST, encoding="utf-8")

    result = CliRunner().invoke(app_module.app, ["manifest", "validate", str(path)])
    assert result.exit_code != 0
    assert "api_tools.expose_destrutive" in result.output


def test_config_lint_missing_file_is_usage_error(tmp_path) -> None:
    missing = tmp_path / "nope.yml"
    result = CliRunner().invoke(app_module.app, ["config", "lint", str(missing)])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_config_lint_without_manifest_still_checks_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_MANIFEST_PATH", raising=False)
    exit_code, output = _config_lint_in_a_clean_process()
    assert exit_code == 0, output
    assert "required settings resolve" in output


def test_config_lint_flags_unresolved_required_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    # A required setting whose env var is absent must fail the lint loudly.
    from tai42_kit.settings import SettingsClassInfo, SettingsFieldInfo

    field = SettingsFieldInfo(
        name="token",
        env_var="TAI_LINT_TEST_REQUIRED",
        type="str",
        default=None,
        required=True,
        secret=False,
        description="",
        nested_group=None,
        accepted_env_vars=["TAI_LINT_TEST_REQUIRED"],
    )
    group = SettingsClassInfo(
        name="LintProbe",
        module="probe",
        qualname="probe.LintProbe",
        fields=[field],
        env_prefix="TAI_LINT_TEST_",
        env_prefix_owned=False,
    )

    import tai42_skeleton.cli.offline as offline_cmd

    monkeypatch.setattr(offline_cmd, "registered_settings", lambda: [group])
    monkeypatch.setattr(offline_cmd, "load_api_routes", list)
    monkeypatch.delenv("TAI_LINT_TEST_REQUIRED", raising=False)

    result = CliRunner().invoke(app_module.app, ["config", "lint"])
    assert result.exit_code != 0
    assert "required settings do not resolve" in result.output


def test_validate_manifest_file_rejects_unreadable_yaml(tmp_path) -> None:
    from tai42_skeleton.cli.offline import validate_manifest_file

    bad = tmp_path / "manifest.yml"
    bad.write_text("key: [unterminated\n", encoding="utf-8")
    with pytest.raises(typer.BadParameter, match="could not read manifest YAML"):
        validate_manifest_file(str(bad))


def test_validate_manifest_file_rejects_non_mapping(tmp_path) -> None:
    from tai42_skeleton.cli.offline import validate_manifest_file

    scalar = tmp_path / "manifest.yml"
    scalar.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(typer.BadParameter, match="YAML mapping"):
        validate_manifest_file(str(scalar))


def test_validate_manifest_file_refuses_oauth_connector_missing_client_env(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An offline lint over a manifest carrying a live oauth connector whose client-credential
    # env is unset is refused by the cli/offline switch to refuse_unresolved_env (the connector
    # half), naming the var — before Manifest.model_validate, so the phantom credential never
    # slips through a cold offline check.
    from tai42_skeleton.cli.offline import validate_manifest_file

    monkeypatch.delenv("ACME_CLIENT_ID", raising=False)
    monkeypatch.delenv("ACME_CLIENT_SECRET", raising=False)
    path = tmp_path / "manifest.yml"
    path.write_text(json.dumps({"connectors": [_OAUTH_CONNECTOR]}), encoding="utf-8")
    with pytest.raises(typer.BadParameter, match="ACME_CLIENT_SECRET"):
        validate_manifest_file(str(path))


def test_config_lint_refuses_oauth_connector_missing_client_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The same refusal surfaces through the ``tai config lint`` door as a non-zero exit.
    monkeypatch.delenv("ACME_CLIENT_ID", raising=False)
    monkeypatch.delenv("ACME_CLIENT_SECRET", raising=False)
    path = tmp_path / "manifest.yml"
    path.write_text(json.dumps({"connectors": [_OAUTH_CONNECTOR]}), encoding="utf-8")

    result = CliRunner().invoke(app_module.app, ["config", "lint", str(path)])
    assert result.exit_code != 0
    assert "ACME_CLIENT_SECRET" in result.output


def test_validate_manifest_file_rejects_invalid_manifest_shape(tmp_path) -> None:
    # A well-formed YAML mapping that violates the ``Manifest`` model surfaces the
    # model's validation error, not a raw traceback.
    from tai42_skeleton.cli.offline import validate_manifest_file

    invalid = tmp_path / "manifest.yml"
    invalid.write_text("mcp: not-a-list\n", encoding="utf-8")
    with pytest.raises(typer.BadParameter, match="invalid manifest"):
        validate_manifest_file(str(invalid))


def test_config_lint_treats_an_empty_manifest_path_as_unset(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # The settings layer's empty-is-absent rule: an exported empty TAI_MANIFEST_PATH names no file.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TAI_MANIFEST_PATH", "")
    exit_code, output = _config_lint_in_a_clean_process()
    assert exit_code == 0, output
    assert "skipping manifest shape check" in output


_DISCOVERED_CONNECTOR = {
    "id": "remote",
    "display_name": "Remote",
    "kind": "oauth",
    "origin": "community",
    "category": "dev",
    "sub_services": {
        "main": {
            "id": "main",
            "display_name": "Main",
            "mcp_server": {"type": "http", "url": "https://mcp.example.com/mcp"},
        }
    },
}


def test_validate_manifest_file_accepts_a_discovered_stored_client_connector(tmp_path) -> None:
    from tai42_skeleton.cli.offline import validate_manifest_file

    path = tmp_path / "manifest.yml"
    path.write_text(json.dumps({"connectors": [_DISCOVERED_CONNECTOR]}), encoding="utf-8")
    validate_manifest_file(str(path))


def test_validate_manifest_file_refuses_a_discovered_env_client_connector_missing_its_env(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_skeleton.cli.offline import validate_manifest_file

    monkeypatch.delenv("REMOTE_ID", raising=False)
    monkeypatch.delenv("REMOTE_SECRET", raising=False)
    pinned = {
        **_DISCOVERED_CONNECTOR,
        "issuer": "https://auth.example.com",
        "client_id_env": "REMOTE_ID",
        "client_secret_env": "REMOTE_SECRET",
    }
    path = tmp_path / "manifest.yml"
    path.write_text(json.dumps({"connectors": [pinned]}), encoding="utf-8")
    with pytest.raises(typer.BadParameter, match="REMOTE_SECRET"):
        validate_manifest_file(str(path))
