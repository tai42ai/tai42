"""Command-line client for operating a tai42 server over its HTTP API.

:func:`app_context` is the public seam for a command mounted from outside this
package (a server-side native command group) to read the invocation's
:class:`~tai42_cli.context.AppContext`.

The package root imports nothing: the ``tai`` entry may have to run a process
prelude before any other import (:mod:`tai42_cli.process_prelude`), and running
that module imports this one first. :func:`app_context` is so resolved on first
access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tai42_cli.commands._common import app_context

__all__ = ["app_context"]


def __getattr__(name: str) -> Any:
    """Resolve :func:`app_context` from its module on first access."""
    if name == "app_context":
        from tai42_cli.commands._common import app_context

        return app_context
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
