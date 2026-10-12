"""Every ``tai42_skeleton`` module imports on its own as the first skeleton import of a process.

The package promises that a subpackage imports on its own ("Import the subpackage
you need"). An in-process import proves nothing once any earlier test has
imported the app, so the check runs in a fresh interpreter
(:mod:`isolated_import_driver`) that imports each module first in its own forked
child; a module that registers on the app at import is imported again with a
stand-in app bound, and must then import completely.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_DRIVER = Path(__file__).with_name("isolated_import_driver.py")


def test_every_skeleton_module_imports_on_its_own_in_a_fresh_interpreter() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(_DRIVER)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["checked"] > 0
    failures = report["failures"]
    assert not failures, "\n\n".join(f"{name}:\n{error}" for name, error in sorted(failures.items()))
