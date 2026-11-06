"""Console-script resolution and direct-spawn probes for the boot engine: the
absolute ``tai``/``uvicorn`` paths a child launch env needs and a refuse-to-boot
spawn for the boot-rules scenarios."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path


def venv_console_script(name: str) -> str:
    """The absolute path to a console script in the active venv (next to the running
    interpreter). An absolute command needs no PATH in a child's launch env, so a stdio
    child resolves it regardless of how its launcher shapes that env. A missing script is
    a mis-provisioned env (the package's wheel is not installed), caught loudly here
    rather than as a cryptic child-spawn failure at boot."""
    candidate = Path(sys.executable).parent / name
    if not candidate.exists():
        raise RuntimeError(f"console script {name!r} not found next to the interpreter at {candidate}")
    return str(candidate)


def tai_bin() -> str:
    """The ``tai`` console script from the active venv — the real entrypoint
    production runs, never ``python -c``."""
    return venv_console_script("tai")


def uvicorn_bin() -> str:
    """The ``uvicorn`` console script from the active venv — the server a user runs
    to serve their own embed host app. Ships in the skeleton dep tree."""
    return venv_console_script("uvicorn")


def spawn_expect_refusal(argv: list[str], env: dict[str, str], cwd: str | Path, *, timeout: float = 20.0) -> str:
    """Spawn a process DIRECTLY (outside the stack readiness framework) and require
    it to REFUSE to boot — a nonzero exit within ``timeout`` — returning its stderr
    so the boot-rules scenarios can assert the refused setting is named on it.

    The boot-refusal scenarios drive ``tai serve``/``tai backend`` and a
    factory-string ``uvicorn`` against a bus-requiring config with no
    ``TAI_BUS_REDIS_URL``: the process must exit nonzero, naming the setting. A
    process that SERVES instead of refusing never exits — it (and its worker process
    group) is killed and this raises loudly, since a boot that should have been
    refused is itself the failure."""
    import os
    import signal
    import subprocess

    proc = subprocess.Popen(
        argv,
        env=env,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # It served instead of refusing: reap the whole process group (a serve
        # master forks worker children) and fail loudly.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        # Reaps the process and reads both pipes to their end, which closes them.
        _, stderr = proc.communicate(timeout=5.0)
        raise RuntimeError(
            f"process did not refuse to boot within {timeout}s (it served): {argv!r}\nstderr:\n{stderr}"
        ) from exc
    if proc.returncode == 0:
        raise RuntimeError(f"process was expected to refuse to boot but exited 0: {argv!r}\nstderr:\n{stderr}")
    return stderr
