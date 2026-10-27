"""``tai42-backend-probe``: the exec probe that reads a backend process's liveness file.

It imports only the liveness settings and file modules, so one run costs a fraction of a ``tai``
invocation. It reads the process environment only, never an env file in the working directory: an
orchestrator's exec probe runs with the container's environment, and a stale env file there would
point it at another file or another grace.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence

from tai42_kit.liveness.file import check_liveness
from tai42_kit.liveness.settings import BackendLivenessSettings
from tai42_kit.settings import env_file_reads_suspended


def _grace(text: str) -> float:
    """``--grace``: zero or more seconds; argparse exits 2 on anything else."""
    value = float(text)
    if not (math.isfinite(value) and value >= 0):
        raise argparse.ArgumentTypeError(f"--grace must be zero or more seconds, got {text}")
    return value


def _settings_from_the_environment() -> BackendLivenessSettings:
    """The liveness settings read from the process environment, with the group's env file unread."""
    env_file = BackendLivenessSettings.tai_env_file
    if env_file is None:
        return BackendLivenessSettings()
    with env_file_reads_suspended(env_file):
        return BackendLivenessSettings()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tai42-backend-probe",
        description=(
            "Exit 0 while the backend process's liveness file (TAI_BACKEND_LIVENESS_PATH) promises a pass that is "
            "not yet overdue by more than the grace, 1 otherwise."
        ),
    )
    parser.add_argument(
        "--grace",
        type=_grace,
        default=None,
        help="Seconds allowed past the promised pass (default: TAI_BACKEND_LIVENESS_GRACE).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 while the liveness file's promise is not overdue past the grace, 1 otherwise; prints one line."""
    args = _parser().parse_args(argv)
    settings = _settings_from_the_environment()
    grace = settings.grace if args.grace is None else args.grace
    result = check_liveness(settings.path, grace)
    sys.stdout.write(f"{result.describe()}\n")
    return 0 if result.alive else 1
