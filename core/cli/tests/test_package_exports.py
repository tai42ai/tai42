"""The CLI package's public surface for code that mounts its own commands."""

from __future__ import annotations

import tai42_cli
from tai42_cli.commands import _common


def test_app_context_is_exported_from_the_package() -> None:
    assert "app_context" in tai42_cli.__all__
    assert tai42_cli.app_context is _common.app_context
