"""How a stdio MCP server child is launched: where its bare command is found, and where its stderr goes.

The owner of the install locations declares one :data:`StdioLaunch` provider through
:func:`declare_stdio_launch` in every process that launches a stdio server: a callable
returning the commands installed server environments expose, each command name mapped to
the executable it runs. An environment exposes only the console scripts its own package
declares — never its interpreter or a dependency's scripts. The provider is called at every
resolution, so a command that appears while the process runs (a server package installed
after boot) is found without a new declaration. Nothing declared means no exposed commands:
a bare command is searched on a ``PATH`` alone.

A command that carries a directory part (an absolute or a relative path) is launched as
given. :func:`resolve_stdio_command` finds a bare name at the first executable match, in
this order:

1. the server's own ``env`` ``PATH``, when it sets one;
2. the exposed command of exactly that name;
3. this process's ``PATH``, only when the server's ``env`` sets no ``PATH``.

A bare name with no executable match raises: :class:`PermissionError` naming each match
that is not an executable file, else :class:`FileNotFoundError`; both name the ``PATH``
searched and the exposed command names.

The child is launched with its ``env`` exactly as the server declared it. A console script
carries its own interpreter in its shebang, so a script of a self-contained environment
imports from that environment with nothing added to the child's environment. The mcp SDK
gives the child only a short safe-list of this process's environment (``HOME``, ``PATH``,
``USER``, …) overlaid with the server config's ``env``, so the config ``env`` is the only
channel into the child's environment, and a ``PATH`` it sets replaces the inherited one.

A child's stderr is this process's standard error descriptor (:func:`stdio_child_stderr`),
whatever object stands at ``sys.stderr``: a host may replace ``sys.stderr`` with a stream
that has no file descriptor (a task worker's logging proxy), and a child cannot be handed
such a stream.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import TextIO

#: A provider of the commands installed server environments expose — command name to the
#: executable it runs — called at every resolution.
type StdioLaunch = Callable[[], Mapping[str, str]]


def _no_exposed_commands() -> Mapping[str, str]:
    """The provider in force until one is declared: no exposed commands."""
    return {}


_LAUNCH: StdioLaunch = _no_exposed_commands


def declare_stdio_launch(provider: StdioLaunch) -> None:
    """Declare the stdio launch provider for this process.

    The owner of the install locations calls it; a later declaration replaces the earlier one.
    """
    global _LAUNCH
    _LAUNCH = provider


def declared_stdio_launch() -> StdioLaunch:
    """The stdio launch provider declared in this process (one exposing no commands when none was declared)."""
    return _LAUNCH


def resolve_stdio_command(command: str, env: Mapping[str, str]) -> str:
    """Return the executable a stdio server launched as ``command`` with ``env`` runs.

    A command with a directory part is returned unchanged. A bare name resolves to the
    first executable match on ``env``'s ``PATH`` when it sets one, then the command of that
    name the declared provider exposes now, then on this process's ``PATH`` when ``env``
    sets none. With no executable match, raises :class:`PermissionError` when a match is
    not an executable file, else :class:`FileNotFoundError`, naming the ``PATH`` searched
    and the exposed command names.
    """
    if os.path.dirname(command):
        return command
    entry_path = "PATH" in env
    path_dirs = os.get_exec_path({"PATH": env["PATH"]} if entry_path else None)
    exposed = dict(_LAUNCH())
    # A PATH directory without the name is no match; an exposed name always is one, so an
    # exposed executable that is missing or not executable is named in the error.
    on_path = [path for path in (os.path.join(directory, command) for directory in path_dirs) if os.path.isfile(path)]
    exposed_match = [exposed[command]] if command in exposed else []
    path_owner = "the server env" if entry_path else "this process's"
    path_text = f"{path_owner} PATH {path_dirs}"
    exposed_text = f"the exposed commands {sorted(exposed)}"
    if entry_path:
        candidates, searched = [*on_path, *exposed_match], f"{path_text}, then {exposed_text}"
    else:
        candidates, searched = [*exposed_match, *on_path], f"{exposed_text}, then {path_text}"
    not_executable: list[str] = []
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        not_executable.append(candidate)
    if not_executable:
        raise PermissionError(
            f"stdio MCP server command {command!r} has no executable match: {not_executable} "
            f"are not executable files (searched {searched})"
        )
    raise FileNotFoundError(f"stdio MCP server command {command!r} was not found in {searched}")


def stdio_child_stderr() -> TextIO:
    """Return the stream a stdio server child's stderr is written to: this process's standard error descriptor.

    A stream over file descriptor 2 that leaves the descriptor open when it is closed. The child
    writes where this process's own standard error goes, whatever object stands at
    ``sys.stderr``. A process whose descriptor 2 is not open raises :class:`OSError`.

    The stream is built with :func:`open` (CPython's :func:`io.open`), not :func:`os.fdopen`,
    because a green process prelude replaces :func:`os.fdopen` with a stream that owns and closes
    its descriptor whatever ``closefd`` says, and sets it non-blocking.
    """
    return open(2, "w", closefd=False)
