"""The status a killed exec ends with: the wrapper records its argv's status, and the sibling kill reads it.

For a provider whose exec wrapper and sibling kill are built on
:mod:`~tai42_kit.sandbox.exec_group_scripts`. A runtime may withhold an exec's own status while
a process the argv started still holds the exec's output, and an exec whose kill closes its
stream reads no status from it. So the wrapper records the status its argv ended with as soon
as it has reaped it (:func:`record_argv_status`), and a kill that signalled the argv's group
reads that status and prints it as a line of its own before its finding
(:func:`report_argv_status`): a killed exec ends with the status its argv really ended with,
the killed status or the argv's own when it ended by itself first, whether or not a process
still holds its output.

A status always reaches a kill that signalled the group while the wrapper runs: the wrapper
keeps the status it recorded until its own end (:func:`end_with_argv_status`), where, once it
has removed the pidfile, it writes that status into the kill's mark when a kill has left one,
and the kill leaves its mark before it reads the pidfile. A kill that reads the pidfile thus
finds the recorded status, or the status in its mark. The kill fails, with one line on its
stderr and exit 1, only when no status comes within :data:`STATUS_WAIT_SECONDS` of its signal,
as for a wrapper stopped or ended from outside before it could record one: that status cannot
be known.

All three run as ``sh -c SCRIPT <exec_id> ...`` with the same ``directory`` as the provider's
group scripts; the status file is named by the exec's own id, the script's ``$0``. They need the
``mv``, ``rm``, ``cat`` and ``sleep`` (with a fractional time) commands, and ``/proc``.
"""

from __future__ import annotations

from tai42_contract.sandbox import SandboxError

from tai42_kit.sandbox.exec_group_scripts import kill_finding, kill_mark, pidfile
from tai42_kit.sandbox.exec_kill import KillOutcome, KillResult

# How long a kill that signalled the group waits for the argv's status, by the clock of
# ``/proc/uptime`` in hundredths of a second, reading it every poll interval. The wrapper
# records the status as soon as it has reaped the killed argv, and this wait and the kill's own
# wait for the processes it expects to end stay inside the kill's bound
# (:data:`~tai42_kit.sandbox.exec_kill.KILL_TIMEOUT_SECONDS`), so a status that never comes is
# reported as such and not as a kill that did not finish.
_STATUS_WAIT_CENTISECONDS = 200
STATUS_WAIT_SECONDS = _STATUS_WAIT_CENTISECONDS / 100
_STATUS_POLL_SECONDS = 0.01

# The lines a kill that signalled the group prints on its stdout: the argv's status, then its finding.
_SIGNALLED_REPORT_LINES = 2


def _argv_status(directory: str) -> str:
    """The shell word naming the file the wrapper of the exec whose id is the script's ``$0`` records its status in."""
    return pidfile(directory) + ".exited"


def record_argv_status(directory: str) -> str:
    """The wrapper's step right after its start: record the argv's status ``$r``, for a kill to read.

    It follows :func:`~tai42_kit.sandbox.exec_group_scripts.start_in_own_group` at once, before
    the wrapper waits for the processes that hold the exec's output, so a kill meanwhile reads
    it. The status is written ATOMICALLY (write-then-``mv``), so a kill never reads it half
    written; a write that fails reports to the exec's stderr (fd 4).
    """
    status = _argv_status(directory)
    return '{ echo "$r" > ' + status + ".tmp && mv " + status + ".tmp " + status + "; } 2>&4; "


def end_with_argv_status(directory: str) -> str:
    """The wrapper's end: remove the pidfile, leave the status in a kill's mark, and exit with the argv's status.

    Once the pidfile is gone, no later kill reads it, so the status the wrapper recorded is
    then written into the kill's mark when a kill has left one (write-then-``mv``), for a kill
    that read the pidfile before it went, and the recorded status is removed only after that.
    A mark stays once its kill has gone, holding the argv's status: a kill that found nothing
    running, came after the wrapper ended, or read the recorded status just as the wrapper was
    writing the mark. Its ``rm`` and ``mv`` report to the exec's stderr (fd 4).
    """
    mark = kill_mark(directory)
    status = _argv_status(directory)
    return (
        "rm -f " + pidfile(directory) + " 2>&4; "
        "[ -e " + mark + " ] && "
        '{ echo "$r" > ' + mark + ".status && mv " + mark + ".status " + mark + "; } 2>&4; "
        "rm -f " + status + " 2>&4; "
        'exit "$r"'
    )


def report_argv_status(directory: str) -> str:
    """The sibling kill's step once it has signalled the group: print the argv's status as a line of its own.

    It follows :func:`~tai42_kit.sandbox.exec_group_scripts.signal_own_group`, whose ``m`` (the
    mark) and ``ticks`` it uses, and precedes
    :func:`~tai42_kit.sandbox.exec_group_scripts.report_signalled`. It reads the status from the
    mark, or as the wrapper recorded it, every :data:`_STATUS_POLL_SECONDS`, prints it and
    removes the mark. A status that has not come within :data:`STATUS_WAIT_SECONDS` cannot be
    known: the kill writes that as one line on its stderr and exits 1, as every kill that fails.
    """
    unreported = f"the sandbox exec was killed but its exit status was not reported within {STATUS_WAIT_SECONDS:g}s"
    return (
        "x=" + _argv_status(directory) + "; "
        f"ticks; n=$((e + {_STATUS_WAIT_CENTISECONDS})); "
        'until { [ -s "$m" ] && v="$(cat "$m")"; } || { [ -s "$x" ] && v="$(cat "$x" 2>/dev/null)"; }; do '
        f'ticks; [ "$e" -ge "$n" ] && {{ echo \'{unreported}\' >&2; exit 1; }}; sleep {_STATUS_POLL_SECONDS}; '
        'done; echo "$v" && rm -f "$m" || exit 1; '
    )


def kill_report(stdout: bytes) -> KillResult:
    """What a sibling kill that succeeded reported on its ``stdout``: its finding, and the argv's status.

    The finding is the last line (:func:`~tai42_kit.sandbox.exec_group_scripts.kill_finding`);
    a kill that signalled the group printed the status its argv ended with as the one line
    before it (:func:`report_argv_status`), carried as the result's ``exit_code``. A finding the
    kill does not report, and a signalled kill whose ``stdout`` holds no single status line
    before its finding, raise :class:`SandboxError`.
    """
    lines = stdout.splitlines()
    result = kill_finding(lines[-1]) if lines else None
    if result is None:
        raise SandboxError(f"sandbox exec kill reported an unknown finding: {stdout!r}")
    if result.outcome is not KillOutcome.SIGNALLED:
        return result
    unreadable = f"sandbox exec kill reported an unreadable exit status: {stdout!r}"
    if len(lines) != _SIGNALLED_REPORT_LINES:
        raise SandboxError(unreadable)
    try:
        status = int(lines[0].decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SandboxError(unreadable) from exc
    return KillResult(result.outcome, exit_code=status, output_held=result.output_held)
