"""How a stdio MCP server child is launched: where its command is found, and what its interpreter imports.

The owner of the install locations declares one :class:`StdioLaunch` through
:func:`declare_stdio_launch` in every process that launches a stdio server: the
directories plugin installs write console scripts to, in order, each paired with the
directories a script launched from it must import (:class:`ScriptLocation`). Nothing
declared means no locations: a bare command is searched on a ``PATH`` alone and the
child's ``env`` is passed as the server declared it.

A command that carries a directory part (an absolute or a relative path) is launched as
given. :func:`resolve_stdio_command` finds a bare name at the first executable match, in
this order:

1. the server's own ``env`` ``PATH``, when it sets one;
2. the declared locations' scripts directories, in their declared order;
3. this process's ``PATH``, only when the server's ``env`` sets no ``PATH``.

A bare name with no executable match raises: :class:`PermissionError` naming each file
found that is not executable, else :class:`FileNotFoundError`; both name every directory
searched.

A child imports the directories of the location it was launched from. A console script
installed outside the launching interpreter's environment runs with that interpreter but
cannot import its own package unless the child is told where it lives, so a command
found in a location that declares import directories gets them on its ``PYTHONPATH``
(:func:`stdio_child_env`: the server's own ``PYTHONPATH`` entries first, then the
location's). A command found on a ``PATH``, in a location that declares none, or given
as a path is launched with its ``env`` exactly as declared. The mcp SDK gives the child
only a short safe-list of this process's environment (``HOME``, ``PATH``, ``USER``, …)
overlaid with the server config's ``env``, so the config ``env`` is the only channel into
the child's environment, and a ``PATH`` it sets replaces the inherited one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ScriptLocation:
    """One directory plugin installs write console scripts to, and what a script launched from it imports."""

    scripts_dir: str
    #: Put on the child's ``PYTHONPATH``, in order; empty when the interpreter already imports them.
    import_dirs: tuple[str, ...] = ()


@dataclass(frozen=True)
class StdioLaunch:
    """The install locations a bare stdio command is searched in, in order."""

    locations: tuple[ScriptLocation, ...]


@dataclass(frozen=True)
class ResolvedStdioCommand:
    """The executable a stdio server runs, and the import directories its child gets."""

    executable: str
    #: The import directories of the location the command was found in; empty otherwise.
    import_dirs: tuple[str, ...]


_LAUNCH = StdioLaunch(locations=())


def declare_stdio_launch(launch: StdioLaunch) -> None:
    """Declare the stdio launch for this process.

    The owner of the install locations calls it; a later declaration replaces the earlier one.
    """
    global _LAUNCH
    _LAUNCH = launch


def declared_stdio_launch() -> StdioLaunch:
    """The stdio launch declared in this process (no locations when none was declared)."""
    return _LAUNCH


def resolve_stdio_command(command: str, env: Mapping[str, str]) -> ResolvedStdioCommand:
    """Return what a stdio server launched as ``command`` with ``env`` runs.

    A command with a directory part is returned unchanged, with no import directories. A
    bare name resolves to the first executable match on ``env``'s ``PATH`` when it sets
    one, then in the declared locations' scripts directories, then on this process's
    ``PATH`` when ``env`` sets none; a match in a location carries that location's import
    directories, a match on a ``PATH`` none. With no executable match, raises
    :class:`PermissionError` when a file of that name exists but is not executable, else
    :class:`FileNotFoundError`, naming every directory searched.
    """
    if os.path.dirname(command):
        return ResolvedStdioCommand(command, ())
    entry_path = "PATH" in env
    path_dirs = os.get_exec_path({"PATH": env["PATH"]} if entry_path else None)
    on_path: list[tuple[str, tuple[str, ...]]] = [(directory, ()) for directory in path_dirs]
    in_locations = [(location.scripts_dir, location.import_dirs) for location in _LAUNCH.locations]
    path_owner = "the server env" if entry_path else "this process's"
    path_text = f"{path_owner} PATH {path_dirs}"
    locations_text = f"the declared scripts directories {[directory for directory, _ in in_locations]}"
    if entry_path:
        searches, searched = [*on_path, *in_locations], f"{path_text}, then {locations_text}"
    else:
        searches, searched = [*in_locations, *on_path], f"{locations_text}, then {path_text}"
    not_executable: list[str] = []
    for directory, import_dirs in searches:
        candidate = os.path.join(directory, command)
        if not os.path.isfile(candidate):
            continue
        if os.access(candidate, os.X_OK):
            return ResolvedStdioCommand(candidate, import_dirs)
        not_executable.append(candidate)
    if not_executable:
        raise PermissionError(
            f"stdio MCP server command {command!r} has no executable match: {not_executable} "
            f"exist but are not executable (searched {searched})"
        )
    raise FileNotFoundError(f"stdio MCP server command {command!r} was not found in {searched}")


def stdio_child_env(env: Mapping[str, str], import_dirs: Sequence[str]) -> dict[str, str]:
    """Return the ``env`` a stdio server child declared with ``env`` is launched with.

    A copy of ``env``. With ``import_dirs``, its ``PYTHONPATH`` is the server's own
    ``PYTHONPATH`` entries (empty entries dropped), then ``import_dirs``, joined with
    :data:`os.pathsep`; with none, the copy is unchanged. Every other key passes through
    as given.
    """
    child = dict(env)
    if import_dirs:
        own = [entry for entry in child.get("PYTHONPATH", "").split(os.pathsep) if entry]
        child["PYTHONPATH"] = os.pathsep.join([*own, *import_dirs])
    return child
