"""Offline ``manifest validate`` and ``config lint`` — no server required.

Both load a manifest file and run it through the in-repo :class:`Manifest` model;
a valid file is accepted, a broken one is rejected loudly with the model's error and
a non-zero exit. ``config lint`` additionally checks that required registered
settings resolve. No client, database, or Redis is touched.
"""

from __future__ import annotations

import json

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

    result = CliRunner().invoke(app_module.app, ["config", "lint", str(path)])
    assert result.exit_code == 0, result.output
    assert "is valid" in result.output
    assert "required settings resolve" in result.output


def test_config_lint_rejects_broken_manifest(tmp_path) -> None:
    path = tmp_path / "manifest.yml"
    path.write_text(_BROKEN_MANIFEST, encoding="utf-8")

    result = CliRunner().invoke(app_module.app, ["config", "lint", str(path)])
    assert result.exit_code != 0
    assert "invalid manifest" in result.output


def test_config_lint_missing_file_is_usage_error(tmp_path) -> None:
    missing = tmp_path / "nope.yml"
    result = CliRunner().invoke(app_module.app, ["config", "lint", str(missing)])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_config_lint_without_manifest_still_checks_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_MANIFEST_PATH", raising=False)
    result = CliRunner().invoke(app_module.app, ["config", "lint"])
    assert result.exit_code == 0, result.output
    assert "required settings resolve" in result.output


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
    )
    group = SettingsClassInfo(name="LintProbe", module="probe", qualname="probe.LintProbe", fields=[field])

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
