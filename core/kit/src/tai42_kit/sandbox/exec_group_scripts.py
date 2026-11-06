"""The in-container shell that runs an exec in a process group of its own, and the sibling kill of that group.

For a provider whose execs run inside a container through an API that gives no in-container pid
to signal. The provider runs every exec under a wrapper that begins with
:func:`start_in_own_group`: it removes the output identities other execs left that nothing holds
any more, records the exec's own output identity, then starts the argv in a session and process
group of its own (``setsid``) through a starter shell, which records its own pid to a pidfile,
then leads that session and group, whose id is its pid, starts the group's anchor and then
``exec``s the argv. A SIBLING exec whose script begins with :func:`signal_own_group` SIGKILLs
that group. The anchor keeps the group, and its id, while the wrapper runs, so the kill reaches
every process the argv left in its group after the argv itself has exited, as it does while the
argv runs; it ends once the wrapper has ended. The wrapper is outside the group: it survives the
kill, reaps the argv and exits with the argv's status. The anchor is no child of the argv's: the
container's init process (PID 1) reaps it, and so it does every process the argv started that
outlives it. The kill leaves a kill mark before it reads the pidfile, and the starter checks for
the mark after it writes the pidfile, both before and after it leads its group, so a kill that
comes before the argv starts keeps it from ever starting.

The output identity is what the exec's stdout and stderr link to in ``/proc/<pid>/fd`` (a
``pipe:[inode]``, or the path of a fifo), the same text for every process that holds them. The
kill reports, once it is done, whether a process still holds the exec's output, since a process
the command started can keep writing once the kill is done: one that left the group, which the
kill does not reach. It waits for the processes it expects to end (the wrapper, every other
process in the wrapper's session, as the ones its wait for the output's release starts, and the
group the pidfile names), then scans the open files of every process for the identity
(:mod:`~tai42_kit.sandbox.open_file_scan`), reading the file table its threads share: its
leading thread's (``/proc/<pid>/fd``), or a live thread's (``/proc/<pid>/task/<tid>/fd``) once
the leading thread has exited or is exiting. A thread that took a file table of its own, through
a raw ``clone()`` without ``CLONE_FILES``, is not scanned. One of the processes the kill expects
to end still holding the identity once the wait is over counts as a holder too. It reports what
it found as one line of its stdout, read by :func:`kill_finding`: :data:`KILL_SIGNALLED` or
:data:`KILL_SIGNALLED_OUTPUT_HELD` once it signalled the group, :data:`KILL_NOTHING_RUNNING` or
:data:`KILL_NOTHING_RUNNING_OUTPUT_HELD` when nothing was left to kill, and then exits 0; a kill
that fails writes one line on its stderr and exits 1. A process's stdout reaches its reader
before its exit does, so a provider reads the finding as soon as the kill knows it, whatever its
runtime delivers last. The scan reads the processes running as the exec's own user that started
no earlier than the exec's wrapper: a process holds the exec's output only once the command gave
it, and a provider runs the command where it cannot change its user (``no-new-privileges``, no
capabilities). A process among those whose open files it cannot read fails the kill, since
whether it holds the output is unknown, unless it is one the kill expects to end, which counts
as a holder until it ends.

Both scripts run as ``sh -c SCRIPT <exec_id> ...``: the pidfile, the mark and the output
identity are named by the exec's own id, the script's ``$0``, inside a directory the provider
names and the container can write. An output identity is removed once a scan finds no process
holding the exec's output, since none ever will again: by the wrapper once nothing holds it
(:func:`wait_output_released`), by a kill that finds nothing holding it once the wrapper has
ended, or by the start of any later exec in the same directory (:func:`start_in_own_group`),
which scans every identity there, as one a wrapper ended from outside left. One still held
stays, for a kill to read and for a later exec's start to scan again. A provider's wrapper
records the argv's status and ends with it, and its kill reads that status before it ends with
:func:`report_signalled`, by the steps of :mod:`~tai42_kit.sandbox.exec_status_scripts`. They
need ``sh`` in the image, with the ``setsid``, ``mkfifo``, ``mv``, ``rm``, ``cat``, ``kill``,
``readlink``, ``ls`` and ``sleep`` (with a fractional time) commands, and ``/proc``, a container
whose init process reaps the processes reparented to it, and an exec API that starts the wrapper
as a session leader, as runc's and containerd's exec do.
"""

from __future__ import annotations

import signal

from tai42_kit.sandbox.exec_kill import KillOutcome, KillResult
from tai42_kit.sandbox.open_file_scan import OPEN_FILE_SCAN

# The exit status of an exec a kill kept from starting: the starter exits with it, the status
# the wrapper's ``wait`` reports for an argv ended by SIGKILL (128 + 9).
KILLED_EXIT_CODE = 128 + signal.SIGKILL

# The kill's findings, each the whole of the line it reports on its stdout. It signalled the
# exec's group, and no process holds the exec's output once the group is gone; or one still does.
KILL_SIGNALLED = "signalled"
KILL_SIGNALLED_OUTPUT_HELD = "signalled output-held"
# Nothing was left to kill (no pidfile, or no process in the group the pidfile names: none yet,
# its starter not yet leading it, or none left), and no process holds the exec's output; or one
# still does.
KILL_NOTHING_RUNNING = "nothing-running"
KILL_NOTHING_RUNNING_OUTPUT_HELD = "nothing-running output-held"

# How long a scan that cannot yet tell is repeated, every poll interval, by the clock of
# ``/proc/uptime`` in hundredths of a second: the kill waits so for the processes it expects to
# end (the wrapper, which ends once it has reaped the argv and nothing else holds the output,
# every other process in the wrapper's session, as the ones the wrapper's wait starts, and the
# members of the group the pidfile names), and for a process whose open files could not be read, as
# one that is exiting. :func:`wait_output_released` repeats its scan every poll interval for as
# long as a process holds the output.
_HOLDER_WAIT_CENTISECONDS = 200
_HOLDER_POLL_SECONDS = 0.02

# ``one_line TEXT``: TEXT in ``r``, each of its newlines replaced by ``; ``, so a failure a script
# reports as its one stderr line keeps all its text.
_ONE_LINE = (
    'one_line() { r=; y="$1"; while :; do case "$y" in '
    '*"\n"*) r="$r${y%%"\n"*}; "; y="${y#*"\n"}";; *) r="$r$y"; return 0;; esac; done; }; '
)

# The starter: ``sh -c STARTER <pidfile> <mark> <in-group> <argv...>``, a child of the wrapper
# in the wrapper's session and process group, the read end of the wrapper's hold on fd 9. It
# records its own pid ATOMICALLY (write-then-``mv``) while it is still in the wrapper's session,
# then checks the kill mark, and exits with :data:`KILLED_EXIT_CODE` instead of starting the
# argv when a kill came first. Otherwise it ``exec``s ``setsid`` and, under it, a shell that runs
# ``<in-group>`` (:data:`_EXEC_STARTER_IN_GROUP`) with the mark and the argv: it is no
# process-group leader, so ``setsid`` makes it the leader of a session and a process group of
# its own without a fork, and the pid it recorded is that group's id. The kill thus finds the
# starter in the wrapper's session until it leads its group, and in the group the pidfile names
# from then on.
_EXEC_STARTER = (
    f'printf "%s\\n" "$$" > "$0".tmp && mv "$0".tmp "$0" || exit 126; [ -e "$1" ] && exit {KILLED_EXIT_CODE}; '
    'm="$1"; s="$2"; shift 2; exec setsid sh -c "$s" "$m" "$@"'
)

# The starter once it leads its own group: ``sh -c IN_GROUP <mark> <argv...>``. It checks the
# kill mark again, for a kill that read its pid before it led its group, and exits with
# :data:`KILLED_EXIT_CODE` when the mark is there. Otherwise it starts the group's anchor, then
# ``exec``s the argv, which keeps its pid and group and holds no end of the hold. The anchor is
# a shell in the group that reads the hold until its end, which comes once the wrapper has
# ended, its own stdin the hold and its stdout and stderr ``/dev/null`` (``exec``, since a
# builtin's redirections last only for that builtin), so it holds none of the exec's stdin or
# output: it keeps the group, and its id, the exec's after the argv has exited, so a kill still
# reaches every process the argv left in it. It is started from a subshell that exits at once,
# so it is no child of the argv's, which a command waiting for all its children would wait for;
# the container's init process (PID 1) reaps it.
_EXEC_STARTER_IN_GROUP = (
    f'[ -e "$0" ] && exit {KILLED_EXIT_CODE}; ( ( exec <&9 > /dev/null 2>&1 9<&-; read -r x ) & ); exec "$@" 9<&-'
)


def pidfile(directory: str) -> str:
    """The shell word naming the pidfile of the exec whose id is the script's ``$0``, in ``directory``."""
    return directory + '/tai-exec."$0"'


def kill_mark(directory: str) -> str:
    """The shell word naming the kill mark of the exec whose id is the script's ``$0``, in ``directory``."""
    return directory + '/tai-exec."$0".kill'


def _group_hold(directory: str) -> str:
    """The shell word naming the fifo the wrapper of the exec whose id is the script's ``$0`` opens as its hold."""
    return directory + '/tai-exec."$0".hold'


def output_identity(directory: str) -> str:
    """The shell word naming the output identity of the exec whose id is the script's ``$0``, in ``directory``."""
    return directory + '/tai-exec."$0".out'


def start_in_own_group(directory: str) -> str:
    """The wrapper's start: record the output identity, run the argv ``"$@"`` in its own group, its status in ``$r``.

    The wrapper must lead its session, as the provider's exec API starts it, so the session the kill
    expects to end is the wrapper's own (its id the wrapper's pid); a wrapper that leads no session,
    or whose own ``/proc/<pid>/stat`` it cannot read to tell, ends the exec with status 126 and a
    line on its stderr before anything else. It then scans every output identity in ``directory``
    that other execs left (``holders``) and removes each one no process holds any more, as one whose
    wrapper was ended from outside before it could remove it; one still held, or whose holders it
    cannot yet tell, stays for a later scan. An identity it cannot read or remove ends the exec with
    status 126 and a line on its stderr, before the argv starts and before its own identity is
    recorded. It then opens its hold: a fifo it creates, opens for reading and writing on fd 8 and
    for reading on fd 9, then removes, so the hold's read end sees its end once the wrapper, its one
    writer, has ended, however it ends; a hold it cannot open ends the exec with status 126 and a
    line on its stderr, before the argv starts. It then records, ATOMICALLY (write-then-``mv``), its
    own pid, its own start time and what its stdout and stderr link to (a link to a deleted fifo
    read without its `` (deleted)`` mark); an identity it cannot record ends the exec with status
    126 and a line on its stderr, before the argv starts. It then runs the starter as a background
    child, which records its pid and then leads a session and a process group of its own
    (``setsid``), whose id is that pid, the argv's own group, the hold's read end passed to the
    starter alone, for the group's anchor (:data:`_EXEC_STARTER`). The argv reads the exec's stdin
    through fd 3, saved in the foreground, since an async command's stdin is ``/dev/null``; an
    end-of-input on the exec's stdin still reaches it, fd 3 being the same pipe. The exec's stderr
    is saved on fd 4 for the starter and the argv, and the wrapper's own stderr goes to
    ``/dev/null`` from here on: a shell whose waited job ends by a signal reports it there
    (``Killed``), which is not the argv's output. The argv holds none of fds 3, 4, 8 and 9. A step
    the wrapper runs after this that reports to the exec's stderr writes to fd 4 (``2>&4``). The
    pidfile and the mark stay for the wrapper's own end to remove, and the anchor lives until the
    wrapper's end.
    """
    record = output_identity(directory)
    return (
        _holders()
        # The wrapper leads its session, so the session the kill expects to end is the one whose
        # id is the wrapper's pid. Its stat is read in a subshell whose stderr is its stdout, so
        # ``x`` is the session id once read, or the shell's own message for a stat it cannot read.
        + 'if ! x="$(exec 2>&1; ofs_stat /proc/$$/stat && printf \'%s\\n\' "$ofs_stat_sid")"; then one_line "$x"; '
        "printf '%s\\n' \"the sandbox exec cannot tell whether its wrapper is a session leader${r:+: $r}\" >&2; "
        "exit 126; fi; "
        '[ "$x" = "$$" ] || { echo "the sandbox exec\'s wrapper is not a session leader" >&2; exit 126; }; '
        "g=; for o in " + directory + '/tai-exec.*.out; do [ -e "$o" ] || continue; holders; c="$?"; '
        '[ "$c" = 0 ] && { rm -f "$o" || exit 126; }; [ "$c" = 1 ] && exit 126; done; '
        "z=" + _group_hold(directory) + "; "
        'if rm -f "$z" && mkfifo "$z" && command exec 8<>"$z" 9<"$z" && rm -f "$z"; '
        'then :; else echo "the sandbox exec cannot open its hold" >&2; exit 126; fi; '
        "o=" + record + "; "
        'if a="$(readlink /proc/$$/fd/1)" && b="$(readlink /proc/$$/fd/2)" '
        '&& ofs_stat /proc/$$/stat && t="$ofs_stat_start" '
        '&& printf \'%s\\n\' "$$" "$t" "${a% (deleted)}" "${b% (deleted)}" > "$o".tmp '
        '&& mv "$o".tmp "$o"; '
        'then :; else echo "the sandbox exec cannot record its output identity" >&2; exit 126; fi; '
        "exec 3<&0 4>&2 2>/dev/null; sh -c '"
        + _EXEC_STARTER
        + "' "
        + pidfile(directory)
        + " "
        + kill_mark(directory)
        + " '"
        + _EXEC_STARTER_IN_GROUP
        + "'"
        + ' "$@" <&3 2>&4 3<&- 4>&- 8>&- & exec 9<&-; wait "$!"; r=$?; '
    )


def signal_own_group(directory: str) -> str:
    """The sibling kill's start: leave the kill mark, then SIGKILL the process group the exec's pidfile names.

    It defines ``report_by_holders`` (:func:`report_signalled`), binds ``f`` to the pidfile, ``m``
    to the mark and ``o`` to the output identity, and exits 1 when it cannot leave the mark: the
    mark is written by ``true``, a regular builtin, since a failed redirection of a special builtin
    such as ``:`` ends a POSIX shell with its own status first. The starter records its pid BEFORE
    it checks the mark, which it checks both before and after it leads its group, and the kill
    leaves the mark BEFORE it reads the pidfile, so whichever side comes first one of them sees the
    other: either the kill reads the pid and signals the group, or the starter sees the mark and
    never starts the argv. A kill that reads the pid before the starter leads its group finds no
    member in it, and the starter sees the mark once it leads it. The group's anchor keeps it, and
    its id, while the wrapper runs, so a kill after the argv has exited still reaches every process
    the argv left in its group, the anchor with them. It finds nothing running when it found no
    pidfile (the exec has either ended, its wrapper having removed the pidfile, or not yet recorded
    its pid and will see the mark, or its command removed the pidfile itself) or no process in the
    recorded group (none yet, the starter not yet leading it, or none left) (``group_member`` finds
    none in ``/proc/<pid>/stat``, read past its last ``) `` since the command name may hold any
    character, a zombie counted since a kill of the group still finds it), as for a wrapper ended
    from outside. It then reports on its stdout whether a process holds the exec's output,
    :data:`KILL_NOTHING_RUNNING` or :data:`KILL_NOTHING_RUNNING_OUTPUT_HELD`, and exits 0. A
    pidfile that is there but that it cannot open or read fails the kill. A kill that fails
    writes one line on its stderr, the lines of the failure's text joined by ``; ``, and exits 1.
    The script goes on past this only once it has signalled the group, its id in ``g``.
    """
    nothing_running = f"report_by_holders '{KILL_NOTHING_RUNNING}' '{KILL_NOTHING_RUNNING_OUTPUT_HELD}'"
    return (
        _report_by_holders()
        + "f="
        + pidfile(directory)
        + "; m="
        + kill_mark(directory)
        + "; o="
        + output_identity(directory)
        + '; g=; true > "$m" || exit 1; '
        # ``group_member`` returns 0 when a process is in the group ``$p``, its own shell aside.
        'group_member() { for d in /proc/[0-9]*; do [ "${d#/proc/}" = "$$" ] && continue; '
        'ofs_stat "$d"/stat 2>/dev/null || continue; [ "$ofs_stat_pgid" = "$p" ] && return 0; '
        "done; return 1; }; "
        # The group the pidfile names is one the kill expects to end as soon as it reads the
        # pid, since the starter it names leads that group once it leaves the wrapper's session.
        # A group with no member yet has a starter that does not lead it yet, which sees the
        # mark once it does: the kill finds nothing running. The kill looks for a member before
        # it signals, so a starter that comes to lead its group just as a signal fails is not
        # reported as a failed kill.
        # The pidfile is read in a subshell whose stderr is its stdout, as the output identity is
        # (``holders``): it exits 4 for a pidfile not there before its open, or gone once its
        # open failed, which the starter records once and which, once removed, is never
        # recorded again; one there that it cannot open or read fails the kill with one line on
        # its stderr.
        'p="$(exec 2>&1; [ -e "$f" ] || exit 4; command exec 5< "$f" || { [ -e "$f" ] || exit 4; exit 1; }; '
        'cat <&5)" || { [ "$?" = 4 ] && '
        f"{nothing_running}; "
        'one_line "$p"; printf \'%s\\n\' "cannot read the sandbox exec pidfile $f${r:+: $r}" >&2; exit 1; }; '
        f'[ -z "$p" ] && {nothing_running}; g="$p"; group_member || {nothing_running}; '
        'if ! e="$(kill -KILL -"$p" 2>&1)"; then '
        f'group_member || {nothing_running}; one_line "$e"; printf \'%s\\n\' "$r" >&2; exit 1; fi; '
    )


def report_signalled() -> str:
    """The sibling kill's end, once it signalled the group: report whether a process still holds the exec's output.

    It reports :data:`KILL_SIGNALLED` on its stdout when no process holds it, and
    :data:`KILL_SIGNALLED_OUTPUT_HELD` when one does, then exits 0.
    """
    return f"report_by_holders '{KILL_SIGNALLED}' '{KILL_SIGNALLED_OUTPUT_HELD}'"


def wait_output_released() -> str:
    """The wrapper's step, once the argv has ended: wait until no process holds the output, then remove its identity.

    The wrapper is the one process the exec's runtime tracks, and a runtime ends the exec's
    stream at the wrapper's exit, so a wrapper that ends only once the exec is over (the argv
    ended and nothing holds its output) has the stream end only then. It scans (``holders``)
    every :data:`_HOLDER_POLL_SECONDS` while a process holds the output or it cannot tell
    whether one does, as for a process whose open files it cannot read: it has no bound of its
    own, the exec's deadline bounding it, where the kill reports what it finds. It removes the
    identity once nothing holds the output, a removal that fails reporting to the exec's stderr
    (fd 4), and leaves it in place for a later exec's start to report when it cannot read it. It
    runs in the wrapper (``$o`` bound by :func:`start_in_own_group`), with the wrapper's own pid
    skipped as the scan's own shell and its scan defined there too. Each scan runs in a subshell
    that first drops the exec's stdout and stderr (fds 1 and 4, its stderr being ``/dev/null``
    already), and so does the wait's ``sleep``, so no process the wait starts holds the exec's
    output past its first step: a kill's scan reads none of them as a holder, and it expects
    every process the wait starts to end, each in the wrapper's session, at any depth and also
    once the wrapper was ended from outside, so one whose open files it cannot read is none it
    names.
    """
    return (
        'g=; while ( exec > /dev/null 4>&-; holders ); c="$?"; [ "$c" = 2 ] || [ "$c" = 3 ]; do '
        f"sleep {_HOLDER_POLL_SECONDS} > /dev/null 4>&-; done; "
        '[ "$c" = 0 ] && rm -f "$o" 2>&4; '
    )


def _holders() -> str:
    """The shell function ``holders``: whether a process holds the exec's output, by its return status.

    It returns 0 when none does, 3 when one does, and 2 when it cannot yet tell: the only holders
    are processes the kill expects to end, or a process's open files could not all be read, as for a
    process that is exiting, or one that hides them. A process
    the kill expects to end whose open files could not be read counts with its holders, since it
    ends either way; for any other, that process and what the listing said are in ``h``. The
    processes the kill expects to end are the wrapper, every other process in the wrapper's session
    (its id the wrapper's pid ``$w``, the wrapper leading it: the wait's scans and ``sleep``,
    :func:`wait_output_released`, and every process they start, at any depth, the init process their
    parent once the wrapper was ended from outside, and the starter until it leads its own group),
    and the members of the group the kill read in the pidfile (``$g``: the starter once it leads it,
    and the argv it becomes). The starter leads a session of its own (``setsid``) before the argv
    starts, and a process can leave its session only for a new one it leads, never join another, so
    no process the argv started is in the wrapper's session. The processes and their open files are
    read by ``ofs_scan`` (:mod:`~tai42_kit.sandbox.open_file_scan`): those running as the scanning
    process's user that started no earlier than the wrapper, but its own shell and those that have
    ended. An exec whose output identity (``$o``) is not there before its open, or is gone once its
    open failed, has nothing holding its output: one that has not recorded it yet, or whose wrapper
    removed it, which it never records again; that read reports nothing. The identity is read from
    what its open gave, so one removed once opened is read as it was. An identity it cannot open
    that is still there, or cannot read once opened, returns 1 with one line on its stderr, which
    gives the shell's own reason when the shell reported one.
    """
    return (
        _ONE_LINE
        + OPEN_FILE_SCAN
        # ``check_holder``, the scan's visit of one process: 3 for one holding the output that
        # the kill does not expect to end, else 0, with ``q`` set for one it expects to end that
        # holds the output or whose open files it could not read, and ``q`` and ``h`` set for one
        # it does not expect to end whose open files it could not read.
        + 'check_holder() { { [ "$ofs_table" = 0 ] && ! ofs_holds_link "$a" && ! ofs_holds_link "$b"; } && return 0; '
        'if [ "$ofs_pid" = "$w" ] || [ "$ofs_sid" = "$w" ] || [ "$ofs_pgid" = "$g" ]; then '
        "q=1; return 0; fi; "
        '{ ofs_holds_link "$a" || ofs_holds_link "$b"; } && return 3; q=1; h="$ofs_pid: $ofs_fds"; return 0; }; '
        "holders() { "
        'N="\n"; '
        # The identity is read in a subshell whose stderr is its stdout, so a failure leaves the
        # shell's own message in ``x`` and the identity's four lines are cut from ``x`` once read
        # (the ``.`` past them keeps an empty last line). The subshell exits 4 for an identity
        # not there: one not there before its open, or one there then whose open failed and that
        # is gone after it, which the wrapper removed and never records again. Its four lines are
        # read from the descriptor the open gave. The failure is reported from a subshell, which
        # keeps the caller's ``r``.
        'x="$(exec 2>&1; [ -e "$o" ] || exit 4; command exec 5< "$o" || { [ -e "$o" ] || exit 4; exit 1; }; '
        "{ read -r w && read -r t && read -r a && read -r b "
        '&& printf \'%s\\n\' "$w" "$t" "$a" "$b" .; } <&5)" '
        '|| { [ "$?" = 4 ] && return 0; '
        '( one_line "$x"; printf \'%s\\n\' "cannot read the sandbox exec output identity $o${r:+: $r}" >&2 ); '
        "return 1; }; "
        'w="${x%%"$N"*}"; x="${x#*"$N"}"; t="${x%%"$N"*}"; x="${x#*"$N"}"; '
        'a="${x%%"$N"*}"; x="${x#*"$N"}"; b="${x%%"$N"*}"; '
        'q=; h=; ofs_scan "$t" check_holder || return 3; [ -z "$q" ] && return 0; return 2; }; '
    )


def _settled_holders() -> str:
    """The shell function ``settled_holders``: :func:`_holders` repeated while it cannot tell, its status in ``c``.

    It runs ``holders`` every :data:`_HOLDER_POLL_SECONDS` while it returns 2, for at most
    :data:`_HOLDER_WAIT_CENTISECONDS`, and leaves its last status in ``c`` (and, for one that
    still cannot tell, what it could not read in ``h``).
    """
    return (
        _holders()
        # ``ticks`` puts the time since boot, in hundredths of a second, in ``e``.
        + 'ticks() { read -r e j < /proc/uptime; e="${e%.*}${e#*.}"; }; '
        "settled_holders() { "
        f'ticks; n=$((e + {_HOLDER_WAIT_CENTISECONDS})); while holders; c="$?"; [ "$c" = 2 ]; do '
        f'ticks; [ "$e" -ge "$n" ] && return; sleep {_HOLDER_POLL_SECONDS}; done; }}; '
    )


def _report_by_holders() -> str:
    """The shell function ``report_by_holders NONE HELD``: report ``NONE`` if nothing holds the output, else ``HELD``.

    It runs :func:`_settled_holders`, then writes the finding as one line on its stdout and exits 0.
    Nothing holding the output means the wrapper has ended or its argv never starts, so it first
    removes the output identity, which no scan needs again. The holders the kill expects to end (the wrapper, every
    other process in its session, and the group the pidfile names, :func:`_holders`) that remain
    once the scan has settled still hold the output; any other process whose open files still cannot
    be read then fails the kill, since whether it holds the output is unknown: it exits 1 with a
    line on its stderr, as it does for an identity it cannot read or cannot remove. A failure whose
    text spans lines is reported on one, its lines joined by ``; ``.
    """
    return (
        _settled_holders() + "report_by_holders() { "
        'settled_holders; [ "$c" = 0 ] && { rm -f "$o" || exit 1; printf \'%s\\n\' "$1"; exit 0; }; '
        '[ "$c" = 3 ] && { printf \'%s\\n\' "$2"; exit 0; }; [ "$c" = 2 ] || exit 1; '
        '[ -n "$h" ] && { one_line "$h"; '
        "printf '%s\\n' \"cannot read the open files of process $r\" >&2; exit 1; }; "
        "printf '%s\\n' \"$2\"; exit 0; }; "
    )


def kill_finding(line: bytes) -> KillResult | None:
    """What the sibling kill found, given the line it reported on its stdout without its newline.

    ``None`` for a line that names no finding.
    """
    findings = {
        KILL_SIGNALLED: KillResult(KillOutcome.SIGNALLED),
        KILL_SIGNALLED_OUTPUT_HELD: KillResult(KillOutcome.SIGNALLED, output_held=True),
        KILL_NOTHING_RUNNING: KillResult(KillOutcome.NOTHING_RUNNING),
        KILL_NOTHING_RUNNING_OUTPUT_HELD: KillResult(KillOutcome.NOTHING_RUNNING, output_held=True),
    }
    return findings.get(line.decode("utf-8", "replace"))
