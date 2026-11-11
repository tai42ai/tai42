"""The process preludes that reshape threads, and a fresh process to run a script under one.

The two preludes are the thread-reshaping ones the backend plugins declare; under either, a
process's threads share one OS thread. A script runs in its own process so the prelude never
reaches the test session, and within a bound, so a hang fails the test instead of holding it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

RESHAPING_PRELUDES = ["gevent.monkey:patch_all", "eventlet:monkey_patch"]

# Bound on one reshaped process: its prelude's import and one door take well under a second.
PROCESS_BOUND_SECONDS = 30


def run_reshaped(script: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``script`` with ``args`` as ``sys.argv[1:]`` in a fresh process, ``env`` added to this one's environment.

    Fails the test when the process does not finish within :data:`PROCESS_BOUND_SECONDS`.
    """
    try:
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(script), *args],
            capture_output=True,
            text=True,
            timeout=PROCESS_BOUND_SECONDS,
            check=False,
            env={**os.environ, **(env or {})},
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"{args} did not finish within {PROCESS_BOUND_SECONDS} s:\n{exc.stderr}")
