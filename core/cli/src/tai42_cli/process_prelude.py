"""The ``tai`` entry that runs a process prelude before anything else is imported.

A backend runtime may declare a process prelude: a ``module:callable`` that reshapes the
interpreter's blocking primitives and so must run before any other module binds them
(:attr:`tai42_contract.backend.runtime.BackendRuntime.process_prelude`). The process learns
which runtime it launches only after the heavy imports, so the host starts itself again, in
place, through this entry (:func:`relaunch`)::

    python -P -m tai42_cli.process_prelude <module:callable> <the tai arguments>

The caller hands :func:`relaunch` the environment the new image starts with: a value this image
wrote into ``os.environ`` reaches the new one only when that environment carries it.

Run as ``__main__``, it runs the named prelude, records it in the environment
(:data:`~tai42_contract.backend.runtime.PROCESS_PRELUDE_ENV`), and then runs the ``tai`` command
on the rest of the arguments. Only the standard library is imported before the prelude runs; the
package root stays import-light for the same reason.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Mapping

# The module run as ``python -m`` by :func:`relaunch`.
_ENTRY_MODULE = "tai42_cli.process_prelude"


def run(name: str) -> None:
    """Import the module of the ``module:callable`` ``name`` and call the callable with no arguments.

    Raises ``ValueError`` when ``name`` is not ``module:callable``, and lets the import's own error
    or ``AttributeError`` propagate when the module or the callable does not exist.
    """
    module_name, sep, attribute = name.rpartition(":")
    if not sep or not module_name or not attribute:
        raise ValueError(f"a process prelude is named 'module:callable', got {name!r}")
    prelude = getattr(importlib.import_module(module_name), attribute)
    prelude()


def relaunch(name: str, env: Mapping[str, str]) -> NoReturn:
    """Replace this process with the ``tai`` command it runs, started through the prelude ``name``.

    The process keeps its pid and its ``tai`` arguments (``sys.argv[1:]``); the new image starts
    with exactly ``env``, not with this image's ``os.environ``. ``-P`` keeps the working directory
    off the module search path, as it is for the ``tai`` console script. The standard streams are
    flushed first: the new image does not inherit the buffers of this one, and no ``atexit`` hook
    of this image runs.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    # This interpreter, this module and the process's own arguments: nothing reaches a shell.
    os.execve(sys.executable, [sys.executable, "-P", "-m", _ENTRY_MODULE, name, *sys.argv[1:]], dict(env))  # noqa: S606


def _main() -> None:
    """Run the prelude named by the first argument, record it, then run ``tai`` on the rest."""
    if len(sys.argv) < 2:
        raise SystemExit(f"usage: python -P -m {_ENTRY_MODULE} <module:callable> [tai arguments...]")
    name = sys.argv.pop(1)
    run(name)
    # Imported only now: the prelude has run, so the contract binds the reshaped primitives.
    from tai42_contract.backend.runtime import PROCESS_PRELUDE_ENV

    os.environ[PROCESS_PRELUDE_ENV] = name
    from tai42_cli.app import app

    app()


if __name__ == "__main__":
    _main()
