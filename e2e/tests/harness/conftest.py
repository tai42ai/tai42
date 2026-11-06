"""Fixtures shared by the harness's own tests."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def inner_pytest(tmp_path: Path) -> Callable[..., str]:
    """Run a test module's source under pytest in a process of its own, with this plugin by its entry point,
    and give the console output."""

    def run(test: str, *args: str) -> str:
        (tmp_path / "test_inner.py").write_text(test)
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(tmp_path), *args],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            timeout=90,
            check=False,
        ).stdout

    return run
