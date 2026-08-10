"""The assembled ``tai`` app with the server installed: the ``tai.commands`` entry
point contributes the local Typer commands, the runtime launchers, and the offline
validators attached onto the client's own ``config``/``manifest`` groups.
"""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner
from tai42_cli import app as app_module

# Remote client groups (owned by tai42-cli) that must render under ``tai --help``.
_REMOTE_GROUPS = {
    "tools",
    "presets",
    "agents",
    "extensions",
    "connectors",
    "hooks",
    "manifest",
    "mcp",
    "sub-mcp",
    "templates",
    "config",
    "keys",
    "scopes",
    "backup",
    "schedules",
    "obs",
    "traces",
    "interactions",
    # cli-native, always present.
    "completion",
    "version",
}
# Contributed by the server through the entry point.
_NATIVE = {"db", "doctor", "catalog", "openapi"}
_LAUNCHERS = {"serve", "backend", "metrics"}


def test_help_renders_client_and_contributed_commands() -> None:
    result = CliRunner().invoke(app_module.app, ["--help"])

    assert result.exit_code == 0, result.output
    for name in _REMOTE_GROUPS | _NATIVE | _LAUNCHERS:
        assert name in result.output, f"missing command in --help: {name}"


def test_compiled_group_exposes_contributed_commands() -> None:
    commands = set(app_module.app.commands)
    assert commands.issuperset(_REMOTE_GROUPS)
    assert commands.issuperset(_NATIVE)
    assert commands.issuperset(_LAUNCHERS)


def test_contributed_launchers_are_the_original_commands() -> None:
    # The contributed launcher subcommands are the existing click launcher
    # commands, only renamed to their ``tai`` subcommand names.
    from tai42_skeleton.cli import backend, mcp_app, metrics

    assert app_module.app.commands["serve"] is mcp_app.cli
    assert app_module.app.commands["backend"] is backend.main
    assert app_module.app.commands["metrics"] is metrics.main


def test_serve_help_renders() -> None:
    result = CliRunner().invoke(app_module.app, ["serve", "--help"])

    assert result.exit_code == 0, result.output
    assert "--transport" in result.output


def test_version_runs_offline() -> None:
    # ``version`` reads only installed metadata, so it succeeds with no server.
    result = CliRunner().invoke(app_module.app, ["version"])
    assert result.exit_code == 0, result.output


def test_doctor_command_is_registered() -> None:
    result = CliRunner().invoke(app_module.app, ["doctor", "--help"])
    assert result.exit_code == 0, result.output


def test_offline_validators_attach_onto_client_groups() -> None:
    # The server contributes ``config lint`` and ``manifest validate`` onto the
    # client's own ``config``/``manifest`` groups, so both render.
    config_lint = CliRunner().invoke(app_module.app, ["config", "lint", "--help"])
    assert config_lint.exit_code == 0, config_lint.output
    manifest_validate = CliRunner().invoke(app_module.app, ["manifest", "validate", "--help"])
    assert manifest_validate.exit_code == 0, manifest_validate.output


def test_attach_offline_raises_naming_missing_group() -> None:
    # A root group lacking the client's ``config``/``manifest`` groups is a wiring
    # error: the attach must raise loudly, naming the group it could not find.
    from tai42_skeleton.cli import offline
    from tai42_skeleton.cli.local import _attach_offline

    with pytest.raises(RuntimeError, match="manifest"):
        _attach_offline(click.Group("tai"), "manifest", offline.manifest_validate, "validate")


def test_register_raises_when_client_groups_absent() -> None:
    # ``register`` attaches the offline validators onto the client's own groups;
    # running it against a root group without them fails loudly, never silently.
    from tai42_skeleton.cli.local import register

    with pytest.raises(RuntimeError, match="config"):
        register(click.Group("tai"))
