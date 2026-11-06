"""Shell functions that read the open files of the processes in ``/proc``, for a provider's in-container scripts.

:data:`OPEN_FILE_SCAN` is POSIX ``sh`` text that defines them; a script puts it before their
first use. The scripts of :mod:`~tai42_kit.sandbox.exec_group_scripts` define them the same
way, so a provider script that composes those and also scans for a file of its own runs this
one code: defining them again in the same shell, from the same text, changes nothing. A shell
has no local variables, so every function and every variable they use is named with the prefix
``ofs_``; a script that uses no name of its own with that prefix keeps all of its own. They
read the same under any ``IFS`` and with ``set -e`` or ``set -f`` on, and leave the caller's
``IFS`` and options as they were: they split no word on ``IFS``, every command whose failure
they handle runs where ``set -e`` does not end the shell (a condition, or an ``&&`` or ``||``
list), and a function that expands a glob turns pathname expansion on for that expansion alone.

- ``ofs_stat FILE`` reads a ``/proc/<pid>/stat`` (or ``task/<tid>/stat``) and sets
  ``ofs_stat_state`` (its 3rd field), ``ofs_stat_pgid`` (the 5th, the process-group id),
  ``ofs_stat_sid`` (the 6th, the session id), ``ofs_stat_flags`` (the 9th, the kernel's
  flags, in decimal) and ``ofs_stat_start`` (the 22nd, the start time in clock ticks since
  boot). It fails for a file it cannot read. The command name, the 2nd field, may hold any
  character, a newline and ``) `` too, and nothing past it holds a newline, so the fields are
  read from the file's last line, past its last ``) ``.
- ``ofs_read_uid FILE`` puts in ``ofs_uid`` the real uid a ``/proc/<pid>/status`` gives, empty
  when it gives none or cannot be read; the file is readable for every process, whatever its
  dumpable flag. It reads the file to its end, so no read of another file gets what is left of
  it.
- ``ofs_list_fds DIR`` puts in ``ofs_fds`` what ``ls -l DIR``, run in the C locale, writes on
  its stdout and then on its stderr. An entry ``ls`` reports is not there
  (``DIR/<n>: No such file or directory``) is an fd the process closed between ``ls``'s read of
  ``DIR`` and its read of that entry, which holds no file any more. It fails when ``ls`` writes
  anything else on its stderr, as busybox's does for a link it cannot read while it exits 0,
  and, with nothing on its stderr, when ``ls`` fails.
- ``ofs_fd_table DIR`` lists in ``ofs_fds`` (``ofs_list_fds``) the file table the threads of the
  process at ``/proc`` directory ``DIR`` share: its leading thread's ``fd`` while that thread
  is neither a zombie nor exiting, else the ``fd`` of a thread that is neither, the next one
  when a thread ends before or as its listing is read. A thread is read again once listed,
  and a listing counts only when the thread is then still neither: a thread that is exiting
  (the ``PF_EXITING`` flag, 0x4, which its kernel sets before the thread lets go of its files)
  may list its table emptied, as root reads it, while another thread still shares it. It
  returns 0 for a table it read, 1 once the whole thread group has ended (its ``stat`` gone,
  or every thread of it a zombie), and 2 for the table of a thread still there that it could
  not read, what the listing said in ``ofs_fds``, or for a process whose threads still there
  are all exiting, the listing of one of them in ``ofs_fds``. A thread that took a file table
  of its own, through a raw ``clone()`` without ``CLONE_FILES``, is not read. It sets what
  ``ofs_stat`` sets too.
- ``ofs_holds_link LINK`` returns 0 when the listing in ``ofs_fds`` names an open file that
  links to ``LINK`` (a ``pipe:[inode]``, or a path, read with or without its `` (deleted)``
  mark), and 1 otherwise.
- ``ofs_scan SINCE VISIT`` reads the file table (``ofs_fd_table``) of every process running as
  the scanning process's real uid that started no earlier than ``SINCE`` (clock ticks since
  boot, as ``ofs_stat_start`` gives them), but the script's own shell (``$$``) and the
  processes whose thread group has ended. A scan run from a subshell that runs as a process of
  its own visits that subshell, whose pid is its own though ``$$`` names its parent's shell, so
  such a subshell that scans for a file it holds closes it first; a shell may run a subshell
  that is the last command of its script in the shell itself, which the scan skips as ``$$``.
  For each process it calls the shell function ``VISIT``, which sees ``ofs_pid`` (the process's
  pid), ``ofs_pgid`` (its process-group id), ``ofs_sid`` (its session id), ``ofs_fds`` (its
  listing) and ``ofs_table`` (0 for a table it read, 2 for one it could not read or whose
  threads are all exiting, as ``ofs_fd_table`` gives it). ``VISIT`` is a function, since a
  command run apart from the shell sees none of them; it runs under the caller's options and
  ``IFS``. It assigns no ``ofs_`` variable, and of these functions it calls ``ofs_holds_link``
  and ``ofs_stat`` alone, which set only their own outputs (the scan reads the ``ofs_stat_``
  fields anew for each process). A ``VISIT`` that returns non-zero ends the scan with that
  status; the scan returns 0 once it has visited every process.

The functions need ``ls`` and ``/proc``.
"""

from __future__ import annotations

OPEN_FILE_SCAN = (
    # ``ofs_glob_on`` turns pathname expansion on, noting in ``ofs_noglob`` that the caller had it
    # off (``set -f``), and ``ofs_glob_back`` puts the caller's back: a function expands a glob
    # between the two, into its own positional parameters, and runs the rest under the caller's.
    'ofs_glob_on() { ofs_noglob=; case "$-" in *f*) ofs_noglob=1; set +f;; esac; }; '
    'ofs_glob_back() { [ -z "$ofs_noglob" ] || set -f; }; '
    # The fields past the command name are cut off by parameter expansion, which no ``IFS``
    # changes; a space added at the end ends the last field like the others. The 5th field is
    # the first past the 2 from the 3rd on, the 9th the first past the 3 from the 6th on, and
    # the 22nd the first past the 13 from the 9th on.
    'ofs_stat() { ofs_tail=; while IFS= read -r ofs_line; do ofs_tail="$ofs_line"; done < "$1" '
    '&& [ -n "$ofs_tail" ] || return 1; ofs_tail="${ofs_tail##*) } "; '
    'ofs_stat_state="${ofs_tail%% *}"; ofs_tail="${ofs_tail#* * }"; '
    'ofs_stat_pgid="${ofs_tail%% *}"; ofs_tail="${ofs_tail#* }"; '
    'ofs_stat_sid="${ofs_tail%% *}"; ofs_tail="${ofs_tail#* * * }"; '
    'ofs_stat_flags="${ofs_tail%% *}"; ofs_tail="${ofs_tail#* * * * * * * * * * * * * }"; '
    'ofs_stat_start="${ofs_tail%% *}"; }; '
    # The status file's fields are split on its blanks (a space and a tab), whatever ``IFS`` is;
    # a file it cannot open leaves ``ofs_uid`` empty. The file is read to its end: bash reads a
    # file in blocks and seeks back over what its last read left, and when it cannot seek back,
    # as in the status of a process that has ended, it gives that rest to the next read of any
    # file.
    'ofs_read_uid() { ofs_uid=; while IFS=" \t" read -r ofs_key ofs_field ofs_rest; do '
    'case "$ofs_key" in Uid:) ofs_uid="$ofs_field";; esac; done < "$1" || ofs_uid=; }; '
    # ``ofs_closed_only DIR`` returns 0 when every line of ``ofs_err`` reports an entry of ``DIR``
    # that is not there, as ``ls`` reports in the C locale an fd closed between its read of the
    # directory and its read of that entry, and 1 for any other line. The text gets a newline
    # added at its end, so every line ends with one.
    'ofs_closed_only() { ofs_nl="\n"; ofs_rest="$ofs_err$ofs_nl"; while [ -n "$ofs_rest" ]; do '
    'ofs_line="${ofs_rest%%"$ofs_nl"*}"; ofs_rest="${ofs_rest#*"$ofs_nl"}"; '
    'case "$ofs_line" in *"$1/"[0-9]*": No such file or directory") ;; *) return 1;; esac; done; return 0; }; '
    # The stderr of ``ls`` is read apart in an inner substitution, its stdout going to the
    # outer one through fd 3. A listing whose stderr reports only closed fds counts as read,
    # whatever the status ``ls`` exits with for them; with nothing on its stderr, that status
    # decides.
    'ofs_list_fds() { ofs_fds="$( { ofs_status=0; ofs_err="$(LC_ALL=C ls -l "$1" 2>&1 >&3 3>&-)" || ofs_status="$?"; '
    'printf \'%s\' "$ofs_err"; [ -z "$ofs_err" ] || { ofs_status=0; ofs_closed_only "$1" || ofs_status=1; }; '
    'exit "$ofs_status"; } 3>&1 )"; }; '
    # ``ofs_thread_table DIR`` lists the open files of the thread at ``DIR`` (a process's own
    # directory for its leading thread, or a ``task/<tid>``): 0 for a listing read, 2 for one
    # that failed, 3 for a thread that is exiting once listed and 1 for one gone or a zombie by
    # then. A thread is read again past its listing, since a thread that ends as it is listed
    # lists its table emptied: its kernel flags ``PF_EXITING`` (0x4) before it lets go of its
    # files, and keeps it set to its end.
    'ofs_thread_table() { ofs_stat "$1"/stat 2>/dev/null && [ "$ofs_stat_state" != Z ] || return 1; '
    'ofs_listed=0; ofs_list_fds "$1"/fd || ofs_listed=2; '
    'ofs_stat "$1"/stat 2>/dev/null && [ "$ofs_stat_state" != Z ] || return 1; '
    '[ "$(( ofs_stat_flags & 4 ))" = 0 ] || return 3; return "$ofs_listed"; }; '
    # The leading thread is read first, then each of the others while none gives a listing or
    # a failure; the listing of an exiting thread is kept in ``ofs_exiting_fds`` for a process
    # with no other live thread left.
    'ofs_fd_table() { ofs_exiting=; ofs_rc=0; ofs_thread_table "$1" || ofs_rc="$?"; '
    'case "$ofs_rc" in 0|2) return "$ofs_rc";; 3) ofs_exiting=1; ofs_exiting_fds="$ofs_fds";; esac; '
    'ofs_glob_on; set -- "$1"/task/[0-9]*; ofs_glob_back; for ofs_task in "$@"; do '
    'ofs_rc=0; ofs_thread_table "$ofs_task" || ofs_rc="$?"; '
    'case "$ofs_rc" in 0|2) return "$ofs_rc";; 3) ofs_exiting=1; ofs_exiting_fds="$ofs_fds";; esac; done; '
    '[ -n "$ofs_exiting" ] || return 1; ofs_fds="$ofs_exiting_fds"; return 2; }; '
    # ``ofs_nl`` is a newline, ending each line ``ls -l`` lists but the last.
    'ofs_holds_link() { ofs_nl="\n"; case "$ofs_fds" in '
    '*" -> $1"|*" -> $1 (deleted)"|*" -> $1$ofs_nl"*|*" -> $1 (deleted)$ofs_nl"*) return 0;; esac; return 1; }; '
    # The uid is read from ``/proc/self``, the scanning process itself, which a subshell of an
    # exited shell still has.
    'ofs_scan() { ofs_since="$1"; ofs_visit="$2"; ofs_read_uid /proc/self/status; ofs_self_uid="$ofs_uid"; '
    'ofs_glob_on; set -- /proc/[0-9]*; ofs_glob_back; for ofs_dir in "$@"; do '
    'ofs_pid="${ofs_dir#/proc/}"; [ "$ofs_pid" = "$$" ] && continue; '
    'ofs_stat "$ofs_dir"/stat 2>/dev/null || continue; [ "$ofs_stat_start" -lt "$ofs_since" ] && continue; '
    'ofs_pgid="$ofs_stat_pgid"; ofs_sid="$ofs_stat_sid"; '
    'ofs_read_uid "$ofs_dir"/status 2>/dev/null; [ "$ofs_uid" = "$ofs_self_uid" ] || continue; '
    'ofs_table=0; ofs_fd_table "$ofs_dir" || ofs_table="$?"; [ "$ofs_table" = 1 ] && continue; '
    '"$ofs_visit" || return "$?"; done; return 0; }; '
)
