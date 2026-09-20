"""The process-wide tool-rename referee registry behind ``app.tools.register_rename_referee``.

A holder of tool-name references (a plugin, or a platform-internal wiring surface)
registers an async referee that, given the tool name about to be renamed, returns
human-readable descriptions of every live reference that name still has. The rename
gate consults EVERY registered referee for the old name and blocks the rename when
any answer is non-empty; a referee raising fails the rename loudly (never a silent
bypass).

Reset on every ``start()`` (like the write-validator registry) so a reload re-imports
the tool modules and re-registers cleanly, and the platform-internal referees re-arm
through their startup/reload handler. Registering the same provider object twice raises
loudly — a double registration is a bug, never a silent duplicate consult.
"""

from __future__ import annotations

from tai42_contract.tools import ToolRenameReferee


class ToolRenameRefereeRegistry:
    """The process-wide list of registered tool-rename referees."""

    def __init__(self) -> None:
        """Start with no registered referees."""
        self._referees: list[ToolRenameReferee] = []

    def register(self, provider: ToolRenameReferee) -> None:
        """Register one referee, raising if the same provider object is already registered."""
        if provider in self._referees:
            raise ValueError("this rename referee is already registered")
        self._referees.append(provider)

    def all(self) -> list[ToolRenameReferee]:
        """Every registered referee."""
        return list(self._referees)

    def reset(self) -> None:
        """Clear every registered referee."""
        self._referees.clear()
