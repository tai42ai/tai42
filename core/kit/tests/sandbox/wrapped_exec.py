"""A sandbox exec wrapper script run under a real shell for a test, its process groups killed at the test's end."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path

WRAPPED_EXEC_TIMEOUT = 10


@contextlib.asynccontextmanager
async def wrapped_exec(
    script: str, directory: Path, exec_id: str, *argv: str
) -> AsyncIterator[asyncio.subprocess.Process]:
    """Run ``argv`` under the wrapper ``script`` as a session leader, its output on pipes, its groups killed on exit.

    ``script`` keeps its pidfile in ``directory`` (``tai-exec.<exec_id>``). The exit kills the
    wrapper's group and the argv's own group that pidfile names, so a block that fails before a
    kill reached the argv fails with its own error. It then reads the output to its end, so its
    pipes are closed once every process holding them is gone; a process still holding them
    fails the test once :data:`WRAPPED_EXEC_TIMEOUT` passes.
    """
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        script,
        exec_id,
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        yield proc
    finally:
        groups = [proc.pid]
        # A pidfile not yet written, or removed at the wrapper's end, names no group left to kill.
        with contextlib.suppress(FileNotFoundError):
            groups.append(int((directory / f"tai-exec.{exec_id}").read_text()))
        for group in groups:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
        await asyncio.wait_for(proc.communicate(), WRAPPED_EXEC_TIMEOUT)
