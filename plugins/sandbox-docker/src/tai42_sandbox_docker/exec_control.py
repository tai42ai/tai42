"""How every Docker exec is wrapped and killed: the in-container scripts and the sibling kill.

The engine starts an exec as a session leader, yet its exec inspect reports a HOST pid,
which no in-container ``kill`` can signal. So every exec runs under :data:`EXEC_WRAPPER`,
which starts the argv in a process group of its own and records it, and a SIBLING exec
running :data:`KILL_WRAPPER` SIGKILLs that group when a call ends without the exec's own end —
a host-side timeout, a caller's ``kill()``, or a one-shot call that is cancelled or fails
(:func:`kill_exec_group`), its output read over the same exec attachment as every other
exec's (:mod:`~tai42_sandbox_docker.attach`); both are built on the kit's
:mod:`~tai42_kit.sandbox.exec_group_scripts` and :mod:`~tai42_kit.sandbox.exec_status_scripts`.
The wrapper survives the kill, records the argv's status as soon as it has reaped it, and exits
with it, which the engine records as the exec's exit code. A kill that signals the group reports
that recorded status, so a killed exec ends with the status the argv really ended with, even
while a process the command started still holds the exec's output and the engine records no
exit code for it.

The group kill reaches every process the argv started that is still in its group, after the argv
itself has exited too: the group's anchor keeps the group, and its id, until the wrapper ends,
which it does once nothing holds the exec's output either, removing the pidfile. The anchor, and
every process the argv started that outlives it, is reparented to the container's main process,
the engine's init (:func:`~tai42_sandbox_docker.provider.build_container_config`), which reaps
it. Every kill reports whether a process still holds the exec's output once it
is done, from the output identity the wrapper records, as the last line of its stdout
(:func:`~tai42_kit.sandbox.exec_status_scripts.kill_report`). The plugin reads the kill's
stdout up to that line and closes the kill's stream: it never waits for the kill's own end or
its exit code, which the engine may hold behind another reader of the container's output that
has paused.

Both need ``sh`` in the image, with the ``setsid``, ``mkfifo``, ``mv``, ``rm``, ``cat``,
``kill``, ``readlink``, ``ls`` and ``sleep`` (with a fractional time) commands the wrapper and
the kill call, ``/proc``, and a writable ``/tmp``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]
from tai42_contract.sandbox import SandboxError, SandboxStreamChunk
from tai42_kit.sandbox.exec_group_scripts import (
    kill_finding,
    report_signalled,
    signal_own_group,
    start_in_own_group,
    wait_output_released,
)
from tai42_kit.sandbox.exec_kill import KillOutcome, KillProgress, KillResult, kill_failed, within_kill_timeout
from tai42_kit.sandbox.exec_status_scripts import (
    end_with_argv_status,
    kill_report,
    record_argv_status,
    report_argv_status,
)

from tai42_sandbox_docker.exec_status import (
    EngineErrorMapper,
    exec_has_ended,
    exec_not_started,
    inspect_exec,
)

if TYPE_CHECKING:
    from tai42_sandbox_docker.attach import ExecAttacher

# The in-container directory every exec's pidfile, kill mark and output identity live in.
_EXEC_FILES_DIR = "/tmp"  # noqa: S108 — an in-container path, not a host temp write

# Every exec runs under this wrapper, ``sh -c WRAPPER <exec_id> <argv...>``: it runs the argv in
# a process group of its own and waits for it, reaping it, and records the argv's status for a
# kill to read. It then waits while a process the command started still holds the exec's
# output, so the engine, which tracks the wrapper alone, sees the exec end only once it is over.
# It then removes the exec's output identity and the pidfile, leaves the argv's status in a
# kill's mark, removes the status it recorded, and exits with the argv's status
# (:func:`~tai42_kit.sandbox.exec_status_scripts.end_with_argv_status`), its ``rm`` and ``mv``
# reporting to the exec's stderr. A kill while it waits finds the pidfile, kills every process
# still in the argv's group, and reports the argv's status and whether the output is still held.
EXEC_WRAPPER = (
    start_in_own_group(_EXEC_FILES_DIR)
    + record_argv_status(_EXEC_FILES_DIR)
    + wait_output_released()
    + end_with_argv_status(_EXEC_FILES_DIR)
)

# The sibling kill: it SIGKILLs the process group the exec's pidfile names and prints the status
# the argv ended with as a line of its own, or finds nothing running; it then reports on its
# stdout whether a process still holds the exec's output and exits 0
# (:func:`~tai42_kit.sandbox.exec_status_scripts.kill_report`). It writes one line on its stderr
# and exits 1 when the kill failed otherwise, or when no status for the argv it signalled came,
# as for a wrapper ended from outside: that status cannot be known. A command that ends by
# itself just before the kill still leaves the kill succeeding, and the status it reports is the
# command's own. A kill whose exec never runs its wrapper, as for an exec the engine never
# started, a kill that finds nothing running or comes after the wrapper has ended, and a kill
# that read the recorded status just as the wrapper was writing the mark leave the mark in
# ``/tmp``.
KILL_WRAPPER = signal_own_group(_EXEC_FILES_DIR) + report_argv_status(_EXEC_FILES_DIR) + report_signalled()


def wrap_command(exec_id: str, argv: Sequence[str]) -> list[str]:
    """The engine command that runs ``argv`` under :data:`EXEC_WRAPPER` as exec ``exec_id``."""
    return ["sh", "-c", EXEC_WRAPPER, exec_id, *argv]


def kill_command(exec_id: str) -> list[str]:
    """The sibling exec that SIGKILLs the process group of exec ``exec_id``."""
    return ["sh", "-c", KILL_WRAPPER, exec_id]


async def kill_exec_group(
    container: Any, exec_obj: Any, exec_id: str, retype: EngineErrorMapper, attach: ExecAttacher
) -> KillResult:
    """SIGKILL the command's process group of the exec ``exec_id`` (``exec_obj``) through a sibling exec.

    The sibling exec runs on ``container`` and its output is read over the stream ``attach``
    opens for it.

    Returns what the kill found: the group signalled, with the status its argv ended with;
    nothing running (the exec had not yet recorded its pid and so never starts its argv, or it
    has ended); the session
    gone, its container answered by the engine with a 404; the exec ended, which its own
    inspect shows after a kill that could not run (a stopped container refuses every new
    exec); or the exec not started, which its own inspect shows after a kill that could not
    run (the kill's mark keeps its argv from ever starting, and an exec the engine never starts
    never runs the wrapper that removes that mark). A kill that signalled the group or found
    nothing running also reports whether a process the command started still holds the exec's
    output. The kill's finding reaches the handle as soon as the kill knows it, before the
    engine records the kill's own exit. A kill that fails while the exec still runs, a signalled
    exec whose status the kill could not read, or a kill whose follow-up inspect fails, raises
    :class:`SandboxError` carrying the kill's own output or the engine's error; so does a kill
    that has not reported within
    :data:`~tai42_kit.sandbox.exec_kill.KILL_TIMEOUT_SECONDS`, noted with the failure the kill
    met before then, or what it had written on its stderr, a partial line included.
    """
    return await within_kill_timeout(
        lambda progress: _kill_and_confirm(container, exec_obj, exec_id, retype, attach, progress)
    )


async def _kill_and_confirm(
    container: Any,
    exec_obj: Any,
    exec_id: str,
    retype: EngineErrorMapper,
    attach: ExecAttacher,
    progress: KillProgress,
) -> KillResult:
    """Run the sibling kill and return its finding; read the exec's own inspect only after a kill that failed.

    A kill that could not run to its report (the engine refused it, its attach failed, or its
    stream ended with no report) is settled by the exec's inspect when that shows the exec
    ended or never started. A kill that ran and reported its own failure is settled only by an
    exec the engine never started: an exec that has ended may have left a process holding its
    output, which the kill could not tell.

    The kill's stderr reaches ``progress`` as it arrives, and its failure is recorded there
    before the inspect, so a kill that runs past its bound keeps them.
    """
    try:
        report = await _signal_group(container, exec_id, retype, attach, progress)
    except SandboxError as kill_error:
        if _container_not_found(kill_error):
            return KillResult(KillOutcome.SESSION_GONE)
        progress.failed(kill_error)
        return await _settled_by_inspect(kill_error, exec_obj, retype, settling=_SETTLED_AFTER_AN_UNRUN_KILL)
    if report.stream == "stderr":
        kill_error = kill_failed(report.data)
        progress.failed(kill_error)
        return await _settled_by_inspect(kill_error, exec_obj, retype, settling=_SETTLED_AFTER_A_FAILED_KILL)
    return kill_report(report.data)


# What the exec's own inspect may settle a kill that could not run to its report with, and one
# that ran and reported its own failure.
_SETTLED_AFTER_AN_UNRUN_KILL = frozenset({KillOutcome.EXEC_ENDED, KillOutcome.NOT_STARTED})
_SETTLED_AFTER_A_FAILED_KILL = frozenset({KillOutcome.NOT_STARTED})


async def _settled_by_inspect(
    kill_error: SandboxError, exec_obj: Any, retype: EngineErrorMapper, *, settling: frozenset[KillOutcome]
) -> KillResult:
    """What the exec's inspect shows after the kill that failed with ``kill_error``, among ``settling``.

    An inspect that shows none of them raises ``kill_error``; one that fails raises it too,
    noted with the inspect's own error.
    """
    try:
        info = await inspect_exec(exec_obj, retype)
    except SandboxError as inspect_error:
        kill_error.add_note(f"the inspect of the sandbox exec also failed: {inspect_error}")
        raise kill_error from kill_error.__cause__
    shown = _shown_by_inspect(info)
    if shown is None or shown.outcome not in settling:
        raise kill_error
    return shown


def _shown_by_inspect(info: Mapping[str, Any]) -> KillResult | None:
    """What an exec inspect ``info`` shows left to kill: nothing once the exec ended or never started, else ``None``."""
    if exec_has_ended(info):
        return KillResult(KillOutcome.EXEC_ENDED)
    if exec_not_started(info):
        return KillResult(KillOutcome.NOT_STARTED)
    return None


async def _signal_group(
    container: Any, exec_id: str, retype: EngineErrorMapper, attach: ExecAttacher, progress: KillProgress
) -> SandboxStreamChunk:
    """Run the sibling kill exec of exec ``exec_id`` over its attached stream and return its report.

    The report is a chunk of the kill's stream, and the stream is closed once it has arrived:
    its stdout up to and including its first whole line that names a finding
    (:func:`~tai42_kit.sandbox.exec_group_scripts.kill_finding`), the lines before it the
    argv's status, or its first whole line on its stderr, its newline dropped, which is its
    failure. A stream that ends before either raises the kill's failure
    (:func:`~tai42_kit.sandbox.exec_kill.kill_failed`) carrying all its output. Its stderr
    reaches ``progress.stderr`` as it arrives. ``attach``
    raises every error of the attach as :class:`SandboxError`, an engine refusal carrying the
    engine's error as its cause.
    """
    try:
        killer = await container.exec(
            cmd=kill_command(exec_id),
            stdin=False,
            stdout=True,
            stderr=True,
            tty=False,
        )
    except DockerError as exc:
        raise retype(exc) from exc
    attachment = await attach(killer.id)
    try:
        stdout = bytearray()
        output = bytearray()
        while (chunk := await attachment.read()) is not None:
            output.extend(chunk.data)
            if chunk.stream == "stderr":
                progress.stderr.extend(chunk.data)
                line, newline, _ = progress.stderr.partition(b"\n")
                if newline:
                    return SandboxStreamChunk(stream="stderr", data=bytes(line))
            else:
                stdout.extend(chunk.data)
                if (report := _through_the_finding(bytes(stdout))) is not None:
                    return SandboxStreamChunk(stream="stdout", data=report)
    finally:
        attachment.close()
    raise kill_failed(bytes(output))


def _through_the_finding(stdout: bytes) -> bytes | None:
    """The kill's ``stdout`` up to and including its first whole line naming a finding, or ``None`` before one."""
    end = 0
    while (newline := stdout.find(b"\n", end)) != -1:
        if kill_finding(stdout[end:newline]) is not None:
            return stdout[: newline + 1]
        end = newline + 1
    return None


def _container_not_found(error: SandboxError) -> bool:
    """Whether ``error`` wraps the engine's 404 for a container that is not there."""
    cause = error.__cause__
    return isinstance(cause, DockerError) and cause.status == HTTPStatus.NOT_FOUND
