"""An engine exec's status as its inspect reports it: running, not started, or ended with its exit code."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]
from tai42_contract.sandbox import SandboxError

# Re-types an engine :class:`DockerError` as a :class:`SandboxError`: the owning sandbox's own
# ``_engine_error``, which also marks the engine unverified on a connection-level status.
EngineErrorMapper = Callable[[DockerError], SandboxError]

# How long a read of an exec's exit code polls for the engine to record it once the exec's
# attach stream has ended: the exec is finishing, and a few short reads cover the brief window
# before the engine records its exit code, without an unbounded wait.
EXIT_CODE_WAIT_SECONDS = 1.0
_EXIT_CODE_POLL_INTERVAL = 0.02


async def inspect_exec(exec_obj: Any, retype: EngineErrorMapper) -> dict[str, Any]:
    """The engine's inspect of ``exec_obj``; an engine error, as for an exec whose container is gone, raises typed."""
    try:
        return await exec_obj.inspect()
    except DockerError as exc:
        raise retype(exc) from exc


def exec_has_ended(info: Mapping[str, Any]) -> bool:
    """Whether an exec inspect ``info`` shows the exec ended, its exit code recorded."""
    return not info.get("Running", False) and info.get("ExitCode") is not None


def exec_not_started(info: Mapping[str, Any]) -> bool:
    """Whether an exec inspect ``info`` shows the engine has not started the exec: not running, no exit code."""
    return not info.get("Running", False) and info.get("ExitCode") is None


async def read_exit_code(
    exec_obj: Any, retype: EngineErrorMapper, *, within_seconds: float = EXIT_CODE_WAIT_SECONDS
) -> int:
    """Read the exit code the engine records for ``exec_obj``, polling for it for at most ``within_seconds``.

    An exec whose exit code is not recorded by then, inspects included, raises
    :class:`SandboxError`; so does an engine error, as for an exec whose container is gone.
    """
    try:
        async with asyncio.timeout(within_seconds):
            while True:
                info = await inspect_exec(exec_obj, retype)
                if exec_has_ended(info):
                    return int(info["ExitCode"])
                await asyncio.sleep(_EXIT_CODE_POLL_INTERVAL)
    except TimeoutError as exc:
        raise SandboxError(
            f"sandbox exec stream ended, but the engine reported no exit code for the exec within {within_seconds}s"
        ) from exc
