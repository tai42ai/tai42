"""Edge path of the native ``openapi`` command: an invalid emitted spec.

Covers the branch a healthy environment never takes — ``--check`` rejecting a
document that is OpenAPI-tagged yet fails schema validation.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from tai42_cli import app as app_module


def test_openapi_check_rejects_invalid_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    import tai42_skeleton.cli.openapi as builder

    # A 3.1-tagged document missing the required ``paths`` object is detected as
    # OpenAPI yet fails schema validation, so the command's ``--check`` surfaces it.
    monkeypatch.setattr(
        builder, "build_openapi_spec", lambda: {"openapi": "3.1.0", "info": {"title": "t", "version": "1"}}
    )
    result = CliRunner().invoke(app_module.app, ["openapi", "--check"])
    assert result.exit_code != 0
    assert "invalid" in result.output.lower()
