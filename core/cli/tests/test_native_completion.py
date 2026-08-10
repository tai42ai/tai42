"""``tai completion install`` — emits a valid shell completion script."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from tai42_cli import app as app_module


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_install_emits_script(shell: str) -> None:
    result = CliRunner().invoke(app_module.app, ["completion", "install", shell])
    assert result.exit_code == 0, result.output
    # A real click completion script references the program's completion env var.
    assert "_TAI_COMPLETE" in result.output
    assert result.output.strip()


def test_completion_install_rejects_unknown_shell() -> None:
    result = CliRunner().invoke(app_module.app, ["completion", "install", "powershell"])
    assert result.exit_code != 0
    assert "powershell" in result.output.lower()


def test_completion_install_reports_missing_completion_support(monkeypatch: pytest.MonkeyPatch) -> None:
    # A shell click cannot build a completion class for is a loud error, never a
    # silent empty script.
    from tai42_cli import completion as completion_mod

    monkeypatch.setattr(completion_mod, "get_completion_class", lambda shell: None)
    result = CliRunner().invoke(app_module.app, ["completion", "install", "bash"])
    assert result.exit_code != 0
    assert "no completion support" in result.output
