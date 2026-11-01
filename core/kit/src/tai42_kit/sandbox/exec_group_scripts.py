"""The in-container shell that runs an exec in a process group of its own, and the sibling kill of that group.

For a provider whose execs run inside a container through an API that gives no in-container
pid to signal. The provider runs every exec under a wrapper that begins with
:func:`start_in_own_group`: it removes the output identities other execs left that nothing
holds any more, records the exec's own output identity, then starts the argv in a
session and process group of its own (``setsid``) through a starter shell, which records its
own pid, the argv's process-group id, to a pidfile, starts the group's anchor and then
``exec``s the argv. A SIBLING exec whose script begins with :func:`signal_own_group` SIGKILLs
that group. The anchor keeps the group, and its id, while the wrapper runs, so the kill reaches
every process the argv left in its group after the argv itself has exited, as it does while
the argv runs; it ends once the wrapper has ended. The wrapper is outside the group: it
survives the kill, reaps the argv and exits with the argv's status. The anchor is no child of
the argv's: the container's init process (PID 1) reaps it, and so it does every process the
argv started that outlives it. The kill leaves a kill mark before it reads the pidfile, and the
starter checks for the mark after it writes the pidfile, so a kill that comes before the pid
is recorded keeps the argv from ever starting.

The output identity is what the exec's stdout and stderr link to in ``/proc/<pid>/fd`` (a
``pipe:[inode]``, or the path of a fifo), the same text for every process that holds them. The
kill reports, once it is done, whether a process still holds the exec's output, since a process
the command started can keep writing once the kill is done: one that left the group, which the
kill does not reach. It waits for the processes it expects to end
(the wrapper, and the group it killed), then scans ``/proc/[0-9]*/fd`` for the identity; one of
those still holding it once the wait is over counts as a holder too. It reports what it found
as one line of its stdout, read by :func:`kill_finding`: :data:`KILL_SIGNALLED` or
:data:`KILL_SIGNALLED_OUTPUT_HELD` once it signalled the group, :data:`KILL_NOTHING_RUNNING` or
:data:`KILL_NOTHING_RUNNING_OUTPUT_HELD` when nothing was left to kill, and then exits 0; a
kill that fails writes one line on its stderr and exits 1. A process's stdout reaches its
reader before its exit does, so a provider reads the finding as soon as the kill knows it,
whatever its runtime delivers last. The scan reads the processes running as the exec's own user
that started no earlier than the exec's wrapper: a process holds the exec's output only once
the command gave it, and a provider runs the command where it cannot change its user
(``no-new-privileges``, no capabilities). A process among those whose open files it cannot read
fails the kill, since whether it holds the output is unknown.

Both scripts run as ``sh -c SCRIPT <exec_id> ...``: the pidfile, the mark and the output
identity are named by the exec's own id, the script's ``$0``, inside a directory the provider
names and the container can write. An output identity is removed once a scan finds no process
holding the exec's output, since none ever will again: by the wrapper once nothing holds it
(:func:`wait_output_released`), by a kill that finds nothing holding it once the wrapper has
ended, or by the start of any later exec in the same directory (:func:`start_in_own_group`),
which scans every identity there, as one a wrapper ended from outside left. One still held
stays, for a kill to read and for a later exec's start to scan again. A provider's wrapper
records the argv's status and ends with it, and its kill reads that status before it ends with
:func:`report_signalled`, by the steps of :mod:`~tai42_kit.sandbox.exec_status_scripts`.
They need ``sh`` in the image, with
the ``setsid``, ``mkfifo``, ``mv``, ``rm``, ``cat``, ``kill``, ``readlink``, ``ls`` and
``sleep`` (with a fractional time) commands, and ``/proc``, and a container whose init process
reaps the processes reparented to it.
"""

from __future__ import annotations

import signal

from tai42_kit.sandbox.exec_kill import KillOutcome, KillResult

# The exit status of an exec a kill kept from starting: the starter exits with it, the status
# the wrapper's ``wait`` reports for an argv ended by SIGKILL (128 + 9).
KILLED_EXIT_CODE = 128 + signal.SIGKILL

# The kill's findings, each the whole of the line it reports on its stdout. It signalled the
# exec's group, and no process holds the exec's output once the group is gone; or one still does.
KILL_SIGNALLED = "signalled"
KILL_SIGNALLED_OUTPUT_HELD = "signalled output-held"
# Nothing was left to kill (no pidfile, or no process left in the recorded group), and no
# process holds the exec's output; or one still does.
KILL_NOTHING_RUNNING = "nothing-running"
KILL_NOTHING_RUNNING_OUTPUT_HELD = "nothing-running output-held"

# How long a scan that cannot yet tell is repeated, every poll interval, by the clock of
# ``/proc/uptime`` in hundredths of a second: the kill waits so for the processes it expects to
# end (the wrapper, which ends once it has reaped the argv and nothing else holds the output, and
# the members of the group it killed), and for a process whose open files could not be read, as
# one that is exiting. :func:`wait_output_released` repeats its scan every poll interval for as
# long as a process holds the output.
_HOLDER_WAIT_CENTISECONDS = 200
_HOLDER_POLL_SECONDS = 0.02

# ``start_time WORDS``, given ``/proc/<pid>/stat`` past its last ``) `` (the command name may
# hold any character): the process's start time, the 22nd field, in clock ticks since boot.
_START_TIME = 'start_time() { set -- $1; shift 19; echo "$1"; }; '

# ``one_line TEXT``: TEXT in ``r``, each of its newlines replaced by ``; ``, so a failure the kill
# reports as its one stderr line keeps all its text.
_ONE_LINE = (
    'one_line() { r=; y="$1"; while :; do case "$y" in '
    '*"\n"*) r="$r${y%%"\n"*}; "; y="${y#*"\n"}";; *) r="$r$y"; return 0;; esac; done; }; '
)

# The starter: ``sh -c STARTER <pidfile> <mark> <argv...>``, run as the leader of its own
# session and process group, the read end of the wrapper's hold on fd 9. It records its own pid
# ATOMICALLY (write-then-``mv``), checks the kill mark only after that, and exits with
# :data:`KILLED_EXIT_CODE` instead of starting the argv when a kill came first. Otherwise it
# starts the group's anchor, then ``exec``s the argv, which keeps its pid and group and holds
# no end of the hold. The anchor is a shell in the group that reads the hold until its end,
# which comes once the wrapper has ended, its own stdin the hold and its stdout and stderr
# ``/dev/null`` (``exec``, since a builtin's redirections last only for that builtin), so it
# holds none of the exec's stdin or output: it keeps the group, and its id, the exec's after
# the argv has exited, so a kill still reaches every process the argv left in it. It is started
# from a subshell that exits at once, so it is no child of the argv's, which a command waiting
# for all its children would wait for; the container's init process (PID 1) reaps it.
_EXEC_STARTER = (
    f'echo "$$" > "$0".tmp && mv "$0".tmp "$0" || exit 126; [ -e "$1" ] && exit {KILLED_EXIT_CODE}; '
    '( ( exec <&9 > /dev/null 2>&1 9<&-; read -r x ) & ); shift; exec "$@" 9<&-'
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

    It first scans every output identity in ``directory`` that other execs left (``holders``) and
    removes each one no process holds any more, as one whose wrapper was ended from outside
    before it could remove it; one still held, or whose holders it cannot yet tell, stays for a
    later scan. An identity it cannot read or remove ends the exec with status 126 and a line on
    its stderr, before the argv starts and before its own identity is recorded. It then opens
    its hold: a fifo it creates, opens for reading and writing on fd 8 and for reading on fd 9,
    then removes, so the hold's read end sees its end once the wrapper, its one writer, has
    ended, however it ends; a hold it cannot open ends the exec with status 126 and a line on
    its stderr, before the argv starts. It then records, ATOMICALLY (write-then-``mv``), its own
    pid, its own start time and what its stdout and stderr link to (a link to a deleted fifo
    read without its `` (deleted)`` mark); an identity it cannot record ends the exec with
    status 126 and a line on its stderr, before the argv starts. It then runs the starter under
    ``setsid`` as a background child, which is no process-group leader, so ``setsid`` makes it
    one without a fork and the pidfile names the argv's own group, the hold's read end passed to
    the starter alone, for the group's anchor (:data:`_EXEC_STARTER`). The argv reads the exec's
    stdin through fd 3, saved in the foreground, since an async command's stdin is
    ``/dev/null``; an end-of-input on the exec's stdin still reaches it, fd 3 being the same
    pipe. The exec's stderr is saved on fd 4 for the starter and the argv, and the wrapper's own
    stderr goes to ``/dev/null`` from here on: a shell whose waited job ends by a signal reports
    it there (``Killed``), which is not the argv's output. The argv holds none of fds 3, 4, 8 and
    9. A step the wrapper runs after this that reports to the exec's stderr writes to fd 4
    (``2>&4``). The pidfile and the mark stay for the wrapper's own end to remove, and the
    anchor lives until the wrapper's end.
    """
    record = output_identity(directory)
    return (
        _START_TIME
        + _holders()
        + "g=; for o in "
        + directory
        + '/tai-exec.*.out; do [ -e "$o" ] || continue; holders; c="$?"; '
        '[ "$c" = 0 ] && { rm -f "$o" || exit 126; }; [ "$c" = 1 ] && exit 126; done; '
        "z=" + _group_hold(directory) + "; "
        'if rm -f "$z" && mkfifo "$z" && command exec 8<>"$z" 9<"$z" && rm -f "$z"; '
        'then :; else echo "the sandbox exec cannot open its hold" >&2; exit 126; fi; '
        "o=" + record + "; "
        'if a="$(readlink /proc/$$/fd/1)" && b="$(readlink /proc/$$/fd/2)" '
        '&& read -r s < /proc/$$/stat && t="$(start_time "${s##*) }")" '
        '&& printf \'%s\\n\' "$$" "$t" "${a% (deleted)}" "${b% (deleted)}" > "$o".tmp '
        '&& mv "$o".tmp "$o"; '
        'then :; else echo "the sandbox exec cannot record its output identity" >&2; exit 126; fi; '
        "exec 3<&0 4>&2 2>/dev/null; setsid sh -c '"
        + _EXEC_STARTER
        + "' "
        + pidfile(directory)
        + " "
        + kill_mark(directory)
        + ' "$@" <&3 2>&4 3<&- 4>&- 8>&- & exec 9<&-; wait "$!"; r=$?; '
    )


def signal_own_group(directory: str) -> str:
    """The sibling kill's start: leave the kill mark, then SIGKILL the process group the exec's pidfile names.

    It defines ``report_by_holders`` (:func:`report_signalled`), binds ``f`` to the pidfile, ``m``
    to the mark and ``o`` to the output identity, and exits 1 when it cannot leave the mark: the
    mark is written by ``true``, a regular builtin, since a failed redirection of a special
    builtin such as ``:`` ends a POSIX shell with its own status first. The starter records its
    pid BEFORE it checks the mark, and the kill leaves the mark BEFORE it reads the pidfile, so
    whichever side comes first one of them sees the other: either the kill reads the pid and
    signals the group, or the starter sees the mark and never starts the argv. The group's
    anchor keeps it, and its id, while the wrapper runs, so a kill after the argv has exited
    still reaches every process the argv left in its group, the anchor with them. It finds
    nothing running when it found no pidfile (the exec has either ended, its wrapper having
    removed the pidfile, or not yet recorded its pid and will see the mark, or its command
    removed the pidfile itself) or no process left in the recorded group (``group_member``
    finds none in ``/proc/<pid>/stat``, read past the last ``) `` since the command name may
    hold any character), as for a wrapper ended from outside. It then reports on its stdout
    whether a process holds the exec's output, :data:`KILL_NOTHING_RUNNING` or
    :data:`KILL_NOTHING_RUNNING_OUTPUT_HELD`, and exits 0. A kill that fails otherwise writes
    one line on its stderr, the lines of the failure's text joined by ``; ``, and exits 1.
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
        'read -r s 2>/dev/null < "$d"/stat || continue; set -- ${s##*) }; [ "$3" = "$p" ] && return 0; '
        "done; return 1; }; "
        'p="$(cat "$f" 2>/dev/null)"; '
        f'[ -z "$p" ] && {nothing_running}; '
        'if ! e="$(kill -KILL -"$p" 2>&1)"; then '
        f'group_member || {nothing_running}; one_line "$e"; echo "$r" >&2; exit 1; fi; g="$p"; '
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
    ended and nothing holds its output) has the stream end only then. It scans (``holders``) every
    :data:`_HOLDER_POLL_SECONDS` while a process holds the output or it cannot tell whether one
    does, as for a process whose open files it cannot read: it has no bound of its own, the
    exec's deadline bounding it, where the kill reports what it finds. It removes the identity
    once nothing holds the output, a removal that fails reporting to the exec's stderr (fd 4),
    and leaves it in place for a later exec's start to report when it cannot read it. It runs in
    the wrapper (``$o`` bound by :func:`start_in_own_group`), with the wrapper's own pid skipped
    as the scan's own shell and its scan defined there too; neither the scan nor its wait holds
    the exec's output, so a kill's scan meanwhile finds only the processes the command started.
    """
    return (
        'g=; while holders 4>&-; c="$?"; [ "$c" = 2 ] || [ "$c" = 3 ]; do '
        f"sleep {_HOLDER_POLL_SECONDS} > /dev/null 4>&-; done; "
        '[ "$c" = 0 ] && rm -f "$o" 2>&4; '
    )


def _holders() -> str:
    """The shell function ``holders``: whether a process holds the exec's output, by its return status.

    It returns 0 when none does, 3 when one does, and 2 when it cannot yet tell: the only
    holders are the wrapper or members of the group the kill signalled (``$g``), which the kill
    expects to end, or a process's open files could not all be read, as for a process that is
    exiting or closing files as they are listed, or one that hides them; that process and what
    the listing said are in ``h``. An exec that never recorded its
    output identity (``$o``), or whose wrapper removed it, has nothing holding its output, and
    so does one whose identity is gone once its read fails, the wrapper having removed it as it
    was read. It reads the processes whose real uid is its own, read from their ``status``
    (readable whatever a process's dumpable flag), that started no earlier than the wrapper,
    skipping its own shell and zombies. An identity it cannot read that is still there returns
    1 with a line on its stderr.
    """
    return (
        # ``uid FILE`` puts the real uid a ``/proc/<pid>/status`` gives in ``v``: the file is
        # readable for every process, whatever its dumpable flag.
        'uid() { v=; while read -r e v j; do [ "$e" = Uid: ] && return 0; done < "$1"; v=; }; '
        "holders() { "
        # ``N`` is a newline, ending each line ``ls -l`` lists but the last.
        'N="\n"; [ -e "$o" ] || return 0; uid /proc/$$/status; u="$v"; '
        '{ read -r w && read -r t && read -r a && read -r b; } < "$o" '
        '|| { [ -e "$o" ] || return 0; echo "cannot read the sandbox exec output identity $o" >&2; return 1; }; '
        "q=; h=; for d in /proc/[0-9]*; do "
        'i="${d#/proc/}"; [ "$i" = "$$" ] && continue; '
        'read -r s 2>/dev/null < "$d"/stat || continue; s="${s##*) }"; '
        '[ "${s%% *}" = Z ] && continue; set -- $s; k="$3"; shift 19; [ "$1" -lt "$t" ] && continue; '
        'uid "$d"/status 2>/dev/null; [ "$v" = "$u" ] || continue; '
        'x="$(ls -l "$d"/fd 2>&1)"; l="$?"; '
        'case "$x" in *" -> $a"|*" -> $a (deleted)"|*" -> $b"|*" -> $b (deleted)") ;; '
        '*" -> $a$N"*|*" -> $a (deleted)$N"*|*" -> $b$N"*|*" -> $b (deleted)$N"*) ;; '
        '*) [ "$l" = 0 ] || { q=1; h="$i: $x"; }; continue;; esac; '
        'if [ "$i" = "$w" ] || [ "$k" = "$g" ]; then q=1; continue; fi; '
        "return 3; done; "
        '[ -z "$q" ] && return 0; return 2; }; '
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

    It runs :func:`_settled_holders`, then writes the finding as one line on its stdout and
    exits 0. Nothing holding the output means the wrapper has ended too, so it first removes the
    output identity, which no scan needs again. The holders the kill expects to end (the
    wrapper, and the group it signalled) that remain once the scan has settled still hold the
    output; a process whose open files still cannot be read then fails the kill, since whether
    it holds the output is unknown: it exits 1 with a line on its stderr, as it does for an
    identity it cannot read or cannot remove. A failure whose text spans lines is reported on
    one, its lines joined by ``; ``.
    """
    return (
        _ONE_LINE + _settled_holders() + "report_by_holders() { "
        'settled_holders; [ "$c" = 0 ] && { rm -f "$o" || exit 1; echo "$1"; exit 0; }; '
        '[ "$c" = 3 ] && { echo "$2"; exit 0; }; [ "$c" = 2 ] || exit 1; '
        '[ -n "$h" ] && { one_line "$h"; echo "cannot read the open files of process $r" >&2; exit 1; }; '
        'echo "$2"; exit 0; }; '
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
