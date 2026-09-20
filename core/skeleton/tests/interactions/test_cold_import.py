"""A cold import of the interactions package must not deadlock on the circular
import that runs when it is the FIRST ``tai42_skeleton`` import: the package's
helper reaches access_control, which through the app's tool wiring reaches back
into the interactions elicit bridge while the package is still initializing."""

from __future__ import annotations

import subprocess
import sys


def test_importing_the_interactions_package_cold_in_a_clean_interpreter_succeeds() -> None:
    """Runs the import in a SUBPROCESS with a clean interpreter — an in-process
    import test proves nothing once any earlier test has imported the app, so the
    pin only bites when the interactions package is the first import."""
    result = subprocess.run(
        [sys.executable, "-c", "import tai42_skeleton.interactions"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
