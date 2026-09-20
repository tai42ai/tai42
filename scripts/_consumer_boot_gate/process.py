"""Run an external gate step under a hard timeout, failing the gate loudly on a hang.

Every external command the gate shells out to — dependency resolution, a boot-venv install,
a migration, a database seed, a descriptor read, a git query — runs through
:func:`run_gate_step`. A step that overruns its bound is stopped and raised through
:func:`_fail`, naming the step, so a stall becomes an actionable boot-gate failure in
minutes instead of a silent job that runs to its host cancellation limit.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from _consumer_boot_gate.versioning import _fail

# Hard per-step bounds. Generous enough that a legitimate slow resolve, install or
# migration finishes, small enough that a stall fails the gate in minutes rather than
# running the job to its host cancellation limit.
INSTALL_TIMEOUT_S = 1200.0
RERESOLVE_TIMEOUT_S = 900.0
VENV_TIMEOUT_S = 300.0
MIGRATE_TIMEOUT_S = 300.0
VENV_PY_TIMEOUT_S = 120.0
DESCRIPTOR_TIMEOUT_S = 120.0
GIT_TIMEOUT_S = 120.0

# How much of a stopped step's captured output the timeout report carries.
_OUTPUT_TAIL_CHARS = 2000


def _output_tail(stream: str | bytes | None) -> str:
    """The last ``_OUTPUT_TAIL_CHARS`` characters of a captured stream, decoded; empty when none."""
    if not stream:
        return ""
    text = stream.decode(errors="replace") if isinstance(stream, bytes) else stream
    return text.strip()[-_OUTPUT_TAIL_CHARS:]


def run_gate_step(argv: list[str], *, what: str, timeout: float, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run ``argv`` with a hard ``timeout``; a step that overruns fails the gate loudly.

    ``what`` names the step in the timeout error so a stall is actionable, and whatever the
    stopped step had written to a captured stream is printed ahead of the error: it is the only
    record of what the step was doing. Every other keyword argument passes straight through to
    :func:`subprocess.run`.
    """
    try:
        return subprocess.run(argv, timeout=timeout, **kwargs)  # noqa: S603 fixed, trusted argv; no shell and no user input
    except subprocess.TimeoutExpired as stalled:
        for label, stream in (("stdout", stalled.stdout), ("stderr", stalled.stderr)):
            tail = _output_tail(stream)
            if tail:
                print(f"--- {what}: last {label} before the step was stopped ---\n{tail}", file=sys.stderr)
        _fail(
            f"{what} did not finish within {timeout:.0f}s and was stopped — the boot gate fails "
            f"fast instead of hanging. Investigate the consumer's boot or the runner: a real boot "
            f"regression must surface as a boot failure, never a silently cancelled job."
        )
