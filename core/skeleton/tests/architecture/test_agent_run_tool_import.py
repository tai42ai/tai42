"""The agent run-tool binding and the serving core import on their own as a process's first skeleton import.

The agent binding builds its run tool on the tool-binding package, and that package's client-tool
seam reads the sentinel an agent run tool defaults its optional parameters to; neither import may
close a loop back into the other.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["tai42_skeleton.agent.binding", "tai42_skeleton.app.serving_core"])
def test_the_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", f"import {module}"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
