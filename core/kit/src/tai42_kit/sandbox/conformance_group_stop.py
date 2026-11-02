"""The group-stop checks of the sandbox conformance suite: a stop kills what the command left in its group.

A command's process group outlives the command's own process while a process the command
started is still in it. Each check runs a command that starts such a process, which holds the
exec's output and leaves a marker only once it has outlived every bound the check sets, and
then exits at once; it then stops the exec past that exit, by its deadline, a one-shot's or an
interactive exec's, or by ``kill()``, and asserts the stop killed that process: the exec stops
as its stop says, never noting a holder of its output, and once the process's time has
passed, a one-shot exec finds its start marker and not its late one.
"""

from __future__ import annotations

import asyncio
import time

from tai42_contract.sandbox import SandboxExecTimeoutError, SandboxStreamChunk, SandboxStreamExit

from tai42_kit.sandbox.base import ManagedSandbox
from tai42_kit.sandbox.conformance_harness import SandboxConformanceConfig, conformance_spec
from tai42_kit.sandbox.exec_kill import OUTPUT_HELD_NOTE
from tai42_kit.sandbox.session import ManagedSandboxSession

# The ``timeout_seconds`` of the deadline cases' execs.
_LEFT_IN_GROUP_TIMEOUT_SECONDS = 1.0

# The ``timeout_seconds`` of the kill case's exec: far past every bound of the case.
_LEFT_IN_GROUP_KILL_TIMEOUT_SECONDS = 30.0

# How long the process the command leaves in its group sleeps before it leaves its late
# marker, and how long past that the cases wait before they look for the marker.
_LEFT_IN_GROUP_SLEEP_SECONDS = 4.0
_LEFT_IN_GROUP_MARGIN_SECONDS = 2.0

# The line the command writes before it exits, after it started the process it leaves.
_LEFT_IN_GROUP_READY = b"conf-left-ready\n"

# How long the kill case waits, once it has read the command's line, for the command's own
# process to have exited before it kills the exec.
_LEFT_IN_GROUP_EXITED_SECONDS = 0.5

# How long the cases give a stop, and the rest of the output, to end the exec: well within the
# sleep of the process the command left, so only a stop that kills it meets the bound.
_LEFT_IN_GROUP_END_SECONDS = 2.0


def _markers(case: str) -> tuple[str, str]:
    """The start and late markers, in the session's working directory, of the ``case`` case's exec."""
    return (f".tai42-conformance-left-{case}-started", f".tai42-conformance-left-{case}-late")


def _left_in_group_argv(case: str) -> list[str]:
    """The ``case`` case's exec: it leaves its start marker and a process in its group, writes a line, and exits.

    The process it leaves holds the exec's output, sleeps, then leaves the late marker.
    """
    started, late = _markers(case)
    return [
        "sh",
        "-c",
        f"touch {started}; (sleep {_LEFT_IN_GROUP_SLEEP_SECONDS}; touch {late}) & "
        f"echo {_LEFT_IN_GROUP_READY.decode().strip()}; exit 0",
    ]


def _left_in_group_probe_argv(case: str) -> list[str]:
    """The one-shot exec reading the ``case`` case's markers: 3 if the exec never started, 4 if its process ran on."""
    started, late = _markers(case)
    return ["sh", "-c", f"test -e {started} || exit 3; test -e {late} && exit 4; exit 0"]


async def check_one_shot_deadline_kills_what_the_command_left_in_its_group(
    sandbox: ManagedSandbox, config: SandboxConformanceConfig
) -> None:
    """An ``exec`` whose command exited, leaving a process in its group holding its output, is killed at its deadline.

    The exec is running while that process holds its output, so at its deadline it raises
    :class:`SandboxExecTimeoutError`, within :data:`_LEFT_IN_GROUP_END_SECONDS` of it, noting no
    holder of its output: the deadline's stop killed the process, which never leaves its late
    marker.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        started = time.monotonic()
        try:
            async with asyncio.timeout(_LEFT_IN_GROUP_TIMEOUT_SECONDS + _LEFT_IN_GROUP_END_SECONDS):
                result = await session.exec(
                    _left_in_group_argv("one-shot"), timeout_seconds=_LEFT_IN_GROUP_TIMEOUT_SECONDS
                )
        except SandboxExecTimeoutError as exc:
            _assert_no_holder_noted(exc, "a one-shot exec")
        except TimeoutError as exc:
            raise AssertionError(
                "a one-shot exec whose command left a process in its group did not raise its timeout"
                f" within {_LEFT_IN_GROUP_END_SECONDS}s of its deadline"
            ) from exc
        else:
            raise AssertionError(
                "a one-shot exec whose command left a process in its group holding its output returned"
                f" exit {result.exit_code} instead of raising its timeout"
            )
        await _assert_left_process_killed(session, "one-shot", started, "by a one-shot exec's deadline")
    finally:
        await session.destroy()


async def check_interactive_deadline_kills_what_the_command_left_in_its_group(
    sandbox: ManagedSandbox, config: SandboxConformanceConfig
) -> None:
    """An interactive exec whose command exited, leaving a process in its group, is killed at its deadline.

    As :func:`check_one_shot_deadline_kills_what_the_command_left_in_its_group`, for an exec
    started with ``exec_start`` and read to its end: its output raises the timeout, noting no
    holder of its output, and the process never leaves its late marker.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        started = time.monotonic()
        handle = await session.exec_start(
            _left_in_group_argv("interactive"), timeout_seconds=_LEFT_IN_GROUP_TIMEOUT_SECONDS
        )
        try:
            try:
                async with asyncio.timeout(_LEFT_IN_GROUP_TIMEOUT_SECONDS + _LEFT_IN_GROUP_END_SECONDS):
                    items = [item async for item in handle.output]
            except SandboxExecTimeoutError as exc:
                _assert_no_holder_noted(exc, "an interactive exec")
            except TimeoutError as exc:
                raise AssertionError(
                    "an interactive exec whose command left a process in its group did not raise its timeout"
                    f" within {_LEFT_IN_GROUP_END_SECONDS}s of its deadline"
                ) from exc
            else:
                raise AssertionError(
                    "an interactive exec whose command left a process in its group holding its output ended"
                    f" with {items[-1:]!r} instead of raising its timeout"
                )
        finally:
            await handle.kill()
        await _assert_left_process_killed(session, "interactive", started, "by an interactive exec's deadline")
    finally:
        await session.destroy()


async def check_kill_kills_what_the_command_left_in_its_group(
    sandbox: ManagedSandbox, config: SandboxConformanceConfig
) -> None:
    """A ``kill()`` once the command has exited kills the process it left in its group, and ends the output.

    The exec's command writes a line and exits, leaving a process in its group that holds its
    output; once the line is read and the command has exited, ``kill()`` returns and the output
    ends with one exit item within :data:`_LEFT_IN_GROUP_END_SECONDS`, and the process never
    leaves its late marker.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        started = time.monotonic()
        handle = await session.exec_start(
            _left_in_group_argv("kill"), timeout_seconds=_LEFT_IN_GROUP_KILL_TIMEOUT_SECONDS
        )
        output = handle.output
        try:
            async with asyncio.timeout(_LEFT_IN_GROUP_END_SECONDS):
                read = b""
                while _LEFT_IN_GROUP_READY not in read:
                    item = await anext(output)
                    if isinstance(item, SandboxStreamChunk):
                        if item.stream == "stdout":
                            read += item.data
                        continue
                    raise AssertionError(
                        "an exec whose command left a process in its group holding its output ended"
                        f" with {item!r} before it was killed"
                    )
        except TimeoutError as exc:
            raise AssertionError(
                f"an exec's command did not write its line within {_LEFT_IN_GROUP_END_SECONDS}s"
            ) from exc
        await asyncio.sleep(_LEFT_IN_GROUP_EXITED_SECONDS)
        try:
            async with asyncio.timeout(_LEFT_IN_GROUP_END_SECONDS):
                await handle.kill()
                rest = [item async for item in output]
        except TimeoutError as exc:
            raise AssertionError(
                "a kill() of an exec whose command left a process in its group did not end its output"
                f" within {_LEFT_IN_GROUP_END_SECONDS}s"
            ) from exc
        exits = [item for item in rest if isinstance(item, SandboxStreamExit)]
        if len(exits) != 1 or rest[-1:] != exits:
            raise AssertionError(
                "a kill() of an exec whose command left a process in its group did not end its output"
                f" with one exit item: {rest[-1:]!r}"
            )
        await _assert_left_process_killed(session, "kill", started, "by kill()")
    finally:
        await session.destroy()


def _assert_no_holder_noted(timeout: SandboxExecTimeoutError, what: str) -> None:
    """Fail when ``timeout`` notes a holder of the output: the stop was to kill every process holding it."""
    if OUTPUT_HELD_NOTE in getattr(timeout, "__notes__", ()):
        raise AssertionError(
            f"{what} whose command left a process in its group timed out noting that a process still"
            " held its output: its deadline's stop did not kill that process"
        ) from timeout


async def _assert_left_process_killed(session: ManagedSandboxSession, case: str, started: float, how: str) -> None:
    """Once the ``case`` case's left process would have left its late marker, assert it did not.

    ``started`` is when the case started its exec (:func:`time.monotonic`); ``how`` names the
    stop in the failure.
    """
    remaining = started + _LEFT_IN_GROUP_SLEEP_SECONDS + _LEFT_IN_GROUP_MARGIN_SECONDS - time.monotonic()
    await asyncio.sleep(max(remaining, 0.0))
    probe = await session.exec(_left_in_group_probe_argv(case), timeout_seconds=30)
    if probe.exit_code == 3:
        raise AssertionError(f"the {case} group-stop case's exec never left its start marker")
    if probe.exit_code == 4:
        raise AssertionError(
            f"a process an exec's command left in its group was not killed {how}: it ran on and left its late marker"
        )
    if probe.exit_code != 0:
        raise AssertionError(
            f"the {case} group-stop case's marker probe exited {probe.exit_code}: {probe.stderr.strip()}"
        )
