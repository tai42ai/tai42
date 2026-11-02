"""The deadline checks of the sandbox conformance suite: what an exec's ``timeout_seconds`` does.

Each check drives a live :class:`~tai42_kit.sandbox.base.ManagedSandbox`'s execs through one
behaviour every consumer relies on: a read past the deadline of an exec ended within it (its
output past what the provider's transport buffers and a reader slower than any grace included),
a kill past the exec's deadline, a one-shot and an interactive exec's timeout, the output that
timeout counts, read or not, the deadline's kill of an exec nothing reads and of one that closed
its output, and the timeout of an exec whose output a process it started still holds at its
deadline, its command exited or running on.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

from tai42_contract.sandbox import (
    SandboxError,
    SandboxExecHandle,
    SandboxExecTimeoutError,
    SandboxStreamChunk,
    SandboxStreamExit,
)

from tai42_kit.sandbox.base import ManagedSandbox
from tai42_kit.sandbox.conformance_harness import SandboxConformanceConfig, conformance_spec, stop_descendant
from tai42_kit.sandbox.exec_kill import OUTPUT_HELD_NOTE

# The ``timeout_seconds`` of the exec the kill-after-deadline case lets run past its deadline.
_KILLED_PAST_DEADLINE_TIMEOUT_SECONDS = 1.0

# The ``timeout_seconds`` of the late-read case's exec, which ends well within it and is first
# read once half as long again has passed.
_LATE_READ_TIMEOUT_SECONDS = 2.0

# The stdout the large-output cases' execs write, never read before their deadline: past the
# 128KiB a provider's transport commonly takes ahead of its reader, so the exec's end sits behind
# output the provider has not taken yet, and within that and one 64KiB pipe, so an exec that
# writes all of it unread still ends.
_LARGE_OUTPUT_LEN = 160 * 1024

# The large-output late-read cases' exec, which writes the large output and ends.
_LARGE_OUTPUT_ARGV = ("head", "-c", str(_LARGE_OUTPUT_LEN), "/dev/zero")

# The slow-read case's reader pauses this long after each chunk of output until it has paused
# this long in all: far past the few seconds a provider commonly waits for an ended exec's stream
# to end, so only a provider that waits on the stream alone, never on its reader, gives it all.
_SLOW_READ_PAUSE_SECONDS = 0.5
_SLOW_READ_SECONDS = 8.0

# The large-output timeout-count case's exec, which writes the large output and runs on past its
# deadline.
_LARGE_OUTPUT_THEN_SLEEP_ARGV = ("sh", "-c", f"head -c {_LARGE_OUTPUT_LEN} /dev/zero; exec sleep 60")

# The ``timeout_seconds`` of the interactive-timeout case's exec, and the ``sleep`` it runs: the
# sleep outlives the deadline, and the bound below, many times over.
_TIMED_OUT_EXEC_TIMEOUT_SECONDS = 1.0
_TIMED_OUT_EXEC_ARGV = ("sleep", "60")

# How long the interactive-timeout case's read of its exec's output, started past the deadline,
# gives the provider to raise the timeout: far less than the exec's sleep, so only a provider
# that kills the exec at its deadline meets it.
_TIMED_OUT_EXEC_END_SECONDS = 10.0

# The output the timeout-count case's ``cat`` echoes before its deadline, never read before the
# timeout raises.
_COUNTED_OUTPUT = b"conf-counted\n"

# The ``timeout_seconds`` of the unread-exec cases' exec, how long that exec sleeps past its
# start before it leaves its late marker, and how long past that time the case waits before it
# looks for the marker.
_UNREAD_EXEC_TIMEOUT_SECONDS = 1.0
_UNREAD_EXEC_SLEEP_SECONDS = 2.0
_UNREAD_EXEC_MARGIN_SECONDS = 2.0

# The files, in the session's working directory, the unread-exec cases' exec leaves: one as it
# starts, one once its sleep is over, which only an exec still running past its deadline leaves.
_UNREAD_STARTED_MARKER = ".tai42-conformance-unread-started"
_UNREAD_LATE_MARKER = ".tai42-conformance-unread-late"

# The one-shot exec that reads the unread-exec cases' markers: exit 3 when the exec never
# started, 4 when it ran on past its deadline.
_UNREAD_PROBE_ARGV = (
    "sh",
    "-c",
    f"test -e {_UNREAD_STARTED_MARKER} || exit 3; test -e {_UNREAD_LATE_MARKER} && exit 4; exit 0",
)

# How long the kill-after-deadline case gives ``kill()`` plus the rest of the output to end.
# The exec's pipe-holding descendant outlives this bound many times over, so only a provider
# that ends the output at the kill — never at the holder's exit — meets it.
_KILLED_EXEC_END_SECONDS = 10.0

# The file, in the session's working directory, where the kill-after-deadline exec records
# its pipe-holding descendant's pid.
_PIPE_HOLDER_PIDFILE = ".tai42-conformance-pipe-holder"

# The kill-after-deadline exec: a descendant that leaves the process group (``setsid``) and
# holds the exec's stdout/stderr open, its pid recorded, then a ``cat`` the case kills.
_PIPE_HOLDER_ARGV = (
    "sh",
    "-c",
    f'setsid sleep 120 & echo "$!" > {_PIPE_HOLDER_PIDFILE}; echo conf-ready; exec cat',
)

# The exec that stops the pipe-holding descendant once the kill-after-deadline case is done.
_STOP_PIPE_HOLDER_ARGV = ("sh", "-c", f'kill -KILL "$(cat {_PIPE_HOLDER_PIDFILE})"')

# The file, in the session's working directory, where the output-holder cases' writer records
# its pid.
_OUTPUT_WRITER_PIDFILE = ".tai42-conformance-output-writer"

# How long the output-holder cases' paused reader waits after each chunk.
_OUTPUT_HOLDER_READ_PAUSE_SECONDS = 0.2

# The writer the output-holder cases' execs start: a process that leaves the exec's process
# group (``setsid``), keeps its stdout and writes to it without pause for ever, its pid
# recorded.
_OUTPUT_WRITER = f"setsid sh -c 'echo \"$$\" > {_OUTPUT_WRITER_PIDFILE}; exec cat /dev/zero' &"

# The output-holder cases' execs: one whose command exits at once after it starts the writer,
# and one whose command runs on past its deadline.
_OUTPUT_HELD_AFTER_EXIT_ARGV = ("sh", "-c", f"{_OUTPUT_WRITER} exit 0")
_OUTPUT_HELD_WHILE_RUNNING_ARGV = ("sh", "-c", f"{_OUTPUT_WRITER} exec sleep 60")

# The exec that stops an output-holder case's writer once its run is done. A writer already gone
# is stopped too: once its exec's stream is cut, a write to the output nothing reads may end it.
# A missing pidfile, or a writer still there that the kill cannot signal, fails the stop.
_STOP_OUTPUT_WRITER_ARGV = (
    "sh",
    "-c",
    f'p=$(cat {_OUTPUT_WRITER_PIDFILE}) || exit 1; kill -KILL "$p" 2>/dev/null || ! test -e "/proc/$p"',
)

# The file, in the session's working directory, where the one-shot timeout-count case's writer
# records how many bytes it has written so far, and the bytes it writes at a time: it writes a
# chunk, then records the new total, so a kill leaves the record at most one chunk short of
# what the writer produced.
_WRITTEN_RECORD = ".tai42-conformance-written"
_WRITTEN_CHUNK_LEN = 4096

# The one-shot timeout-count case's exec: it writes to its stdout without pause until its
# deadline's kill, recording its total after each chunk (write-then-``mv``, so a kill never
# leaves the record half written).
_RECORDED_WRITER_ARGV = (
    "sh",
    "-c",
    f"n=0; while :; do head -c {_WRITTEN_CHUNK_LEN} /dev/zero; n=$((n + {_WRITTEN_CHUNK_LEN})); "
    f'echo "$n" > {_WRITTEN_RECORD}.tmp && mv {_WRITTEN_RECORD}.tmp {_WRITTEN_RECORD}; done',
)

# The read-to-end case's exec: its command exits at once with a status of its own, leaving a
# writer outside its process group (``setsid``) that holds its stdout and writes a line a second,
# long enough to outlive any bound a provider puts on the stream of an exec whose process has
# exited; its timeout is far longer than the writer's life.
_HELD_TO_END_LINES = 4
_HELD_TO_END_EXIT_CODE = 3
_HELD_TO_END_ARGV = (
    "sh",
    "-c",
    f'setsid sh -c \'i=1; while :; do echo "$i"; [ "$i" = {_HELD_TO_END_LINES} ] && exit 0; '
    f"i=$((i + 1)); sleep 1; done' & exit {_HELD_TO_END_EXIT_CODE}",
)
_HELD_TO_END_TIMEOUT_SECONDS = 30.0
# How long the read-to-end case's writer holds the exec's output: its first line to its last.
_HELD_TO_END_SECONDS = _HELD_TO_END_LINES - 1


# How each output-holder case reads its exec: ``(pause_seconds, first_read_past_deadline, how)``.
_OUTPUT_HOLDER_READS = (
    (0.0, False, "read as it came"),
    (_OUTPUT_HOLDER_READ_PAUSE_SECONDS, False, "read with pauses"),
    (0.0, True, "first read past its deadline"),
)


async def check_late_read_after_exit(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """An interactive exec that ended within its deadline gives its output and exit however late it is read.

    The exec ends well within its ``timeout_seconds`` and its output is first read once that
    time has long passed: the read yields the whole output and one :class:`SandboxStreamExit`,
    never a :class:`SandboxExecTimeoutError`, since the exec was not running at its deadline.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        handle = await session.exec_start(["cat"], timeout_seconds=_LATE_READ_TIMEOUT_SECONDS)
        await handle.write_stdin(b"conf-late\n")
        await handle.close_stdin()
        await asyncio.sleep(_LATE_READ_TIMEOUT_SECONDS * 1.5)
        try:
            items = [item async for item in handle.output]
        except SandboxExecTimeoutError as exc:
            raise AssertionError("an exec that ended within its deadline timed out when read after it") from exc
        chunks = [item for item in items if isinstance(item, SandboxStreamChunk)]
        stdout = b"".join(chunk.data for chunk in chunks if chunk.stream == "stdout")
        if b"conf-late" not in stdout:
            raise AssertionError("an exec that ended within its deadline lost its output when read after it")
        if items[-1:] != [SandboxStreamExit(exit_code=0)]:
            raise AssertionError("an exec that ended within its deadline did not end with its exit when read after it")
    finally:
        await session.destroy()


async def check_late_read_of_large_output(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """An interactive exec that ended within its deadline gives all its output when read late, past its buffers too.

    The exec writes more stdout than a transport commonly takes ahead of its reader, then ends,
    well within its ``timeout_seconds``; its output is first read once that time has long
    passed. The exec was not running at its deadline, though its end sat behind output not
    taken yet: the read yields every byte it wrote and one :class:`SandboxStreamExit`, never a
    :class:`SandboxExecTimeoutError`.
    """
    await _assert_large_output_read_late(sandbox, config, pause_seconds=0.0, reading="read after it")


async def check_slow_read_of_large_output(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """An interactive exec that ended within its deadline gives all its output to a reader slower than any grace.

    As :func:`check_late_read_of_large_output`, but the reader pauses after each chunk until it
    has spent :data:`_SLOW_READ_SECONDS` away from the output: however long the consumer takes
    between reads, the exec ended in time, so the read yields every byte it wrote and one
    :class:`SandboxStreamExit`, never a :class:`SandboxError`.
    """
    await _assert_large_output_read_late(
        sandbox, config, pause_seconds=_SLOW_READ_PAUSE_SECONDS, reading="read slowly after it"
    )


async def _assert_large_output_read_late(
    sandbox: ManagedSandbox, config: SandboxConformanceConfig, *, pause_seconds: float, reading: str
) -> None:
    """Run the large-output exec, read it once its deadline has long passed, and assert it gave all its output.

    The reader pauses ``pause_seconds`` after each chunk until it has paused
    :data:`_SLOW_READ_SECONDS` in all; ``reading`` says how it read, in the failures.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        handle = await session.exec_start(list(_LARGE_OUTPUT_ARGV), timeout_seconds=_LATE_READ_TIMEOUT_SECONDS)
        await asyncio.sleep(_LATE_READ_TIMEOUT_SECONDS * 1.5)
        items: list[SandboxStreamChunk | SandboxStreamExit] = []
        paused = 0.0
        try:
            async for item in handle.output:
                items.append(item)
                if pause_seconds and paused < _SLOW_READ_SECONDS:
                    await asyncio.sleep(pause_seconds)
                    paused += pause_seconds
        except SandboxError as exc:
            failed = "timed out" if isinstance(exc, SandboxExecTimeoutError) else f"raised {exc!r}"
            raise AssertionError(
                "an exec that ended within its deadline, its output past its transport's buffer, "
                f"{failed} when {reading}"
            ) from exc
        stdout_len = sum(
            len(item.data) for item in items if isinstance(item, SandboxStreamChunk) and item.stream == "stdout"
        )
        if stdout_len != _LARGE_OUTPUT_LEN:
            raise AssertionError(
                f"an exec that ended within its deadline gave {stdout_len} of the {_LARGE_OUTPUT_LEN} stdout bytes"
                f" it wrote when {reading}"
            )
        if items[-1:] != [SandboxStreamExit(exit_code=0)]:
            raise AssertionError(
                "an exec that ended within its deadline, its output past its transport's buffer,"
                f" did not end with its exit when {reading}"
            )
    finally:
        await session.destroy()


async def check_kill_after_deadline(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """A killed interactive exec ends promptly with one exit item, past its deadline included.

    Once ``kill()`` has returned, the kill ended the exec: the rest of its output ends with one
    :class:`SandboxStreamExit` and raises no :class:`SandboxExecTimeoutError`, though its
    ``timeout_seconds`` has passed and a descendant that left its process group still holds its
    stdout/stderr open. The exec is still running when it is killed, so every provider knows
    its status: the case asserts the exit item, never the :class:`SandboxError` a provider
    raises when it cannot read an exec's status. The case then stops that descendant itself,
    since a provider whose execs run as host processes leaves it running past the session.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        handle = await session.exec_start(
            list(_PIPE_HOLDER_ARGV), timeout_seconds=_KILLED_PAST_DEADLINE_TIMEOUT_SECONDS
        )
        try:
            await _assert_killed_exec_ends_with_one_exit(handle)
        finally:
            await stop_descendant(session, _STOP_PIPE_HOLDER_ARGV, "kill-after-deadline")
    finally:
        await session.destroy()


async def _assert_killed_exec_ends_with_one_exit(handle: SandboxExecHandle) -> None:
    """Let ``handle``'s exec pass its deadline, kill it, and assert its output ends with one exit."""
    output = handle.output
    first = await anext(output)
    await asyncio.sleep(_KILLED_PAST_DEADLINE_TIMEOUT_SECONDS * 1.5)
    try:
        async with asyncio.timeout(_KILLED_EXEC_END_SECONDS):
            await handle.kill()
            rest = [item async for item in output]
    except SandboxExecTimeoutError as exc:
        raise AssertionError("an exec killed past its deadline ended in a timeout, not its exit item") from exc
    except TimeoutError as exc:
        raise AssertionError(
            f"a killed exec's output did not end within {_KILLED_EXEC_END_SECONDS}s while a descendant held its pipes"
        ) from exc
    items = [first, *rest]
    exits = [item for item in items if isinstance(item, SandboxStreamExit)]
    if len(exits) != 1 or not isinstance(items[-1], SandboxStreamExit):
        raise AssertionError("an exec killed past its deadline did not end with one exit item")


async def check_exec_timeout(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """An ``exec`` past its ``timeout_seconds`` is killed and raises :class:`SandboxExecTimeoutError`."""
    session = await sandbox.create_session(conformance_spec(config))
    try:
        try:
            await session.exec(["sleep", "5"], timeout_seconds=0.2)
        except SandboxExecTimeoutError:
            pass
        else:
            raise AssertionError("an exec past its timeout was not killed")
    finally:
        await session.destroy()


async def check_exec_timeout_counts_output(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """An ``exec``'s timeout counts every byte the exec produced, those still on their way at the kill included.

    The exec writes to its stdout without pause until its deadline's kill, recording its total
    after each chunk, so output is still on its way to the provider as the deadline passes:
    :class:`SandboxExecTimeoutError` counts no stderr and at least the recorded total, at most
    one chunk more, the chunk the kill may have cut before its record.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        try:
            await session.exec(list(_RECORDED_WRITER_ARGV), timeout_seconds=_TIMED_OUT_EXEC_TIMEOUT_SECONDS)
        except SandboxExecTimeoutError as exc:
            timeout = exc
        else:
            raise AssertionError("an exec writing past its timeout was not killed")
        written = int(await session.get_file(_WRITTEN_RECORD))
        counted = (timeout.stdout_len, timeout.stderr_len)
        if not (written <= counted[0] <= written + _WRITTEN_CHUNK_LEN and counted[1] == 0):
            raise AssertionError(
                f"an exec's timeout counted stdout {counted[0]} and stderr {counted[1]} bytes of the"
                f" {written} to {written + _WRITTEN_CHUNK_LEN} stdout bytes it produced before its kill"
            ) from timeout
    finally:
        await session.destroy()


async def check_interactive_exec_timeout(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """An interactive exec still running at its ``timeout_seconds`` is killed and its ``output`` raises.

    The exec sleeps far past its deadline and its output is first read once the deadline has
    passed: the read raises :class:`SandboxExecTimeoutError` within a bound shorter than the
    sleep, never an exit item and never a wait on the sleep.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        handle = await session.exec_start(list(_TIMED_OUT_EXEC_ARGV), timeout_seconds=_TIMED_OUT_EXEC_TIMEOUT_SECONDS)
        await asyncio.sleep(_TIMED_OUT_EXEC_TIMEOUT_SECONDS * 1.5)
        try:
            async with asyncio.timeout(_TIMED_OUT_EXEC_END_SECONDS):
                items = [item async for item in handle.output]
        except SandboxExecTimeoutError:
            return
        except TimeoutError as exc:
            raise AssertionError(
                f"an interactive exec running past its deadline was not killed: its output did not raise"
                f" its timeout within {_TIMED_OUT_EXEC_END_SECONDS}s"
            ) from exc
        raise AssertionError(
            f"an interactive exec running past its deadline was not killed: its output ended with {items[-1:]!r}"
            " instead of raising its timeout"
        )
    finally:
        await session.destroy()


async def check_interactive_timeout_counts_output(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """An interactive exec's timeout counts the output the exec produced before its kill, read or not.

    ``cat`` echoes a line well before its deadline and runs on, its output never read until the
    deadline has passed: the read raises :class:`SandboxExecTimeoutError` whose stdout and stderr
    lengths are that line's and none.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        handle = await session.exec_start(["cat"], timeout_seconds=_TIMED_OUT_EXEC_TIMEOUT_SECONDS)
        await handle.write_stdin(_COUNTED_OUTPUT)
        await asyncio.sleep(_TIMED_OUT_EXEC_TIMEOUT_SECONDS * 1.5)
        _assert_counted(await _timeout_of(handle), len(_COUNTED_OUTPUT))
    finally:
        await session.destroy()


async def check_interactive_timeout_counts_large_output(
    sandbox: ManagedSandbox, config: SandboxConformanceConfig
) -> None:
    """An interactive exec's timeout counts its output past what its transport buffers, never read.

    The exec writes more stdout than a transport commonly takes ahead of its reader, then runs on
    past its deadline, its output never read until the deadline has passed: the read raises
    :class:`SandboxExecTimeoutError` whose stdout and stderr lengths are every byte it wrote and
    none, the bytes the provider had not taken by the kill included.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        handle = await session.exec_start(
            list(_LARGE_OUTPUT_THEN_SLEEP_ARGV), timeout_seconds=_TIMED_OUT_EXEC_TIMEOUT_SECONDS
        )
        await asyncio.sleep(_TIMED_OUT_EXEC_TIMEOUT_SECONDS * 1.5)
        _assert_counted(await _timeout_of(handle), _LARGE_OUTPUT_LEN)
    finally:
        await session.destroy()


async def _timeout_of(handle: SandboxExecHandle) -> SandboxExecTimeoutError:
    """The timeout the output of ``handle``, an exec running past its deadline, raises within the bound."""
    try:
        async with asyncio.timeout(_TIMED_OUT_EXEC_END_SECONDS):
            items = [item async for item in handle.output]
    except SandboxExecTimeoutError as exc:
        return exc
    except TimeoutError as exc:
        raise AssertionError(
            f"an interactive exec running past its deadline did not raise its timeout within"
            f" {_TIMED_OUT_EXEC_END_SECONDS}s"
        ) from exc
    raise AssertionError(
        f"an interactive exec running past its deadline ended with {items[-1:]!r} instead of raising its timeout"
    )


def _assert_counted(timeout: SandboxExecTimeoutError, stdout_len: int) -> None:
    """Assert ``timeout`` counts ``stdout_len`` stdout bytes and no stderr, the output its exec produced."""
    counted = (timeout.stdout_len, timeout.stderr_len)
    if counted != (stdout_len, 0):
        raise AssertionError(
            f"an interactive exec's timeout counted stdout {counted[0]} and stderr {counted[1]} bytes"
            f" of the {stdout_len} stdout bytes it produced before its kill"
        ) from timeout


def _unread_exec_argv() -> list[str]:
    """The unread-exec case's exec: it leaves its start marker, sleeps past its deadline, then its late marker."""
    return [
        "sh",
        "-c",
        f"touch {_UNREAD_STARTED_MARKER}; sleep {_UNREAD_EXEC_SLEEP_SECONDS}; touch {_UNREAD_LATE_MARKER}",
    ]


def _output_closing_exec_argv() -> list[str]:
    """The closed-output case's exec: the unread-exec case's, its stdout and stderr closed once it has started."""
    return [
        "sh",
        "-c",
        f"touch {_UNREAD_STARTED_MARKER}; exec >/dev/null 2>&1; sleep {_UNREAD_EXEC_SLEEP_SECONDS};"
        f" touch {_UNREAD_LATE_MARKER}",
    ]


async def check_unread_exec_killed_at_deadline(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """An interactive exec nothing reads is killed at its ``timeout_seconds`` all the same.

    The exec leaves a marker as it starts, sleeps past its deadline, then leaves a second one.
    Its output is never read: once the sleep, and a margin, have passed, a one-shot exec finds the
    first marker and not the second, so the exec was killed at its deadline, never by a read.
    """
    await _assert_unread_exec_killed_at_deadline(sandbox, config, _unread_exec_argv(), "nothing read")


async def check_output_closed_exec_killed_at_deadline(
    sandbox: ManagedSandbox, config: SandboxConformanceConfig
) -> None:
    """An interactive exec that closed its output, its command running on, is killed at its deadline.

    The exec leaves a marker as it starts, closes its stdout and stderr, sleeps past its
    deadline, then leaves a second one; its output is never read. The end of its output is not
    the end of the exec: once the sleep, and a margin, have passed, a one-shot exec finds the
    first marker and not the second, so the exec was killed at its deadline.
    """
    await _assert_unread_exec_killed_at_deadline(sandbox, config, _output_closing_exec_argv(), "that closed its output")


async def _assert_unread_exec_killed_at_deadline(
    sandbox: ManagedSandbox, config: SandboxConformanceConfig, argv: list[str], what: str
) -> None:
    """Run the unread exec ``argv``, which leaves the unread-exec markers, and assert its deadline killed it.

    ``what`` names the exec in the failure of one that ran on past its deadline.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        handle = await session.exec_start(argv, timeout_seconds=_UNREAD_EXEC_TIMEOUT_SECONDS)
        try:
            await asyncio.sleep(_UNREAD_EXEC_SLEEP_SECONDS + _UNREAD_EXEC_MARGIN_SECONDS)
            probe = await session.exec(list(_UNREAD_PROBE_ARGV), timeout_seconds=30)
        finally:
            await handle.kill()
        if probe.exit_code == 3:
            raise AssertionError("the unread-exec case's exec never left its start marker")
        if probe.exit_code == 4:
            raise AssertionError(
                f"an interactive exec {what} was not killed at its deadline: it ran on and left its late marker"
            )
        if probe.exit_code != 0:
            raise AssertionError(
                f"the unread-exec case's marker probe exited {probe.exit_code}: {probe.stderr.strip()}"
            )
    finally:
        await session.destroy()


async def check_output_held_after_exit_read_to_end(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """An ``exec`` whose output a process it started holds past the command's exit is read to that output's end.

    The exec's command exits at once with a status of its own, while a writer it started outside
    its group writes a line every second for several seconds: the exec runs while the writer
    holds its output, so the call returns the command's exit code and every line, in order, once
    the writer has ended, well within its timeout.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        started = time.monotonic()
        result = await session.exec(list(_HELD_TO_END_ARGV), timeout_seconds=_HELD_TO_END_TIMEOUT_SECONDS)
        took = time.monotonic() - started
        lines = "".join(f"{i}\n" for i in range(1, _HELD_TO_END_LINES + 1))
        if (result.exit_code, result.stdout) != (_HELD_TO_END_EXIT_CODE, lines):
            raise AssertionError(
                "an exec whose output a process it started held past the command's exit returned"
                f" exit {result.exit_code} and stdout {result.stdout!r}, not exit {_HELD_TO_END_EXIT_CODE}"
                f" and {lines!r}"
            )
        if took < _HELD_TO_END_SECONDS:
            raise AssertionError(
                f"an exec whose output a process it started held for {_HELD_TO_END_SECONDS}s returned in {took:.2f}s"
            )
    finally:
        await session.destroy()


async def check_output_holder_at_deadline_times_out(sandbox: ManagedSandbox, config: SandboxConformanceConfig) -> None:
    """An exec whose output a process it started still holds at its deadline times out, however it is read.

    The exec's command starts a writer that leaves its process group, keeps the exec's stdout
    and writes to it without pause for ever, then exits at once. While a process the command
    started holds its output the exec is still running: at its deadline it times out, whether
    its output is read as it comes, read more slowly than the writer writes, or first read past
    the deadline.
    """
    await _assert_held_output_times_out(sandbox, config, _OUTPUT_HELD_AFTER_EXIT_ARGV, "whose command had exited")


async def check_signalled_exec_with_output_holder_times_out(
    sandbox: ManagedSandbox, config: SandboxConformanceConfig
) -> None:
    """An exec running at its deadline, its output held by a process outside its group, times out however it is read.

    As :func:`check_output_holder_at_deadline_times_out`, but the command runs on past its
    deadline after it starts the writer: the deadline's kill stops the command, never the
    writer, and the exec times out all the same.
    """
    await _assert_held_output_times_out(
        sandbox, config, _OUTPUT_HELD_WHILE_RUNNING_ARGV, "whose command ran on past it"
    )


async def _assert_held_output_times_out(
    sandbox: ManagedSandbox, config: SandboxConformanceConfig, argv: Sequence[str], what: str
) -> None:
    """Run the output-holder exec ``argv`` once per read of :data:`_OUTPUT_HOLDER_READS` and assert each times out.

    Each run's output raises :class:`SandboxExecTimeoutError` within
    :data:`_TIMED_OUT_EXEC_END_SECONDS` of the deadline, counting no fewer stdout bytes than the
    reader received and noted :data:`~tai42_kit.sandbox.exec_kill.OUTPUT_HELD_NOTE`; then
    ``kill()`` returns, and the run stops its writer through a one-shot exec. ``what`` names the
    exec in the failures.
    """
    session = await sandbox.create_session(conformance_spec(config))
    try:
        for pause_seconds, first_read_past_deadline, how in _OUTPUT_HOLDER_READS:
            deadline = asyncio.get_running_loop().time() + _TIMED_OUT_EXEC_TIMEOUT_SECONDS
            handle = await session.exec_start(list(argv), timeout_seconds=_TIMED_OUT_EXEC_TIMEOUT_SECONDS)
            try:
                if first_read_past_deadline:
                    await asyncio.sleep(_TIMED_OUT_EXEC_TIMEOUT_SECONDS * 1.5)
                failure = f"an exec {what}, its output still held by a process it started, {how},"
                timeout, received = await _held_timeout_of(handle, deadline, pause_seconds, failure)
                if timeout.stdout_len < received:
                    raise AssertionError(
                        f"{failure} timed out counting {timeout.stdout_len} stdout bytes of the {received} it gave"
                    ) from timeout
                if OUTPUT_HELD_NOTE not in getattr(timeout, "__notes__", ()):
                    raise AssertionError(
                        f"{failure} timed out without noting that a process still held its output"
                    ) from timeout
                try:
                    async with asyncio.timeout(_KILLED_EXEC_END_SECONDS):
                        await handle.kill()
                except TimeoutError as exc:
                    raise AssertionError(
                        f"{failure} did not return from kill() within {_KILLED_EXEC_END_SECONDS}s of its timeout"
                    ) from exc
            finally:
                await stop_descendant(session, _STOP_OUTPUT_WRITER_ARGV, "output-holder")
    finally:
        await session.destroy()


async def _held_timeout_of(
    handle: SandboxExecHandle, deadline: float, pause_seconds: float, failure: str
) -> tuple[SandboxExecTimeoutError, int]:
    """The timeout ``handle``'s output raises and the stdout bytes received before it.

    The reader pauses ``pause_seconds`` after each chunk. Output that ends instead, with an exit
    item or none, or raises no timeout within :data:`_TIMED_OUT_EXEC_END_SECONDS` of the exec's
    ``deadline`` (a time of the running loop's clock), fails the check.
    """
    received = 0
    ended: SandboxStreamExit | None = None
    try:
        async with asyncio.timeout_at(deadline + _TIMED_OUT_EXEC_END_SECONDS):
            async for item in handle.output:
                if isinstance(item, SandboxStreamExit):
                    ended = item
                    break
                if item.stream == "stdout":
                    received += len(item.data)
                if pause_seconds:
                    await asyncio.sleep(pause_seconds)
    except SandboxExecTimeoutError as exc:
        return exc, received
    except TimeoutError as exc:
        raise AssertionError(
            f"{failure} did not raise its timeout within {_TIMED_OUT_EXEC_END_SECONDS}s of its deadline"
        ) from exc
    raise AssertionError(f"{failure} ended with {ended!r} instead of raising its timeout")
