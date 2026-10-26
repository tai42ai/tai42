"""The process-wide preset-delete referee registry — the body behind ``app.tools.register_delete_referee``.

A holder of resources keyed on a preset/tool name registers an async referee that, given the
set of names a delete removes, VETOES by answering, keyed by name, one ``ReferenceHolder`` per
reference it will not let the delete strand, and changes nothing (the delete preview calls it
too); cleanup belongs in a delete cascade. The delete gate asks EVERY registered referee once,
for the preset and each extension branch tool bound off it together, and blocks the delete when
any answer holds; a referee raising fails the delete loudly (never a silent bypass).

Reset on every ``start()`` (like the rename-referee registry) so a reload re-imports the
tool modules and re-registers cleanly. Registering the same provider object twice raises
loudly — a double registration is a bug, never a silent duplicate consult.
"""

from __future__ import annotations

from tai42_contract.tools import ToolDeleteReferee


class ToolDeleteRefereeRegistry:
    """The process-wide registry of preset-delete referees."""

    def __init__(self) -> None:
        """Start with no registered referees."""
        self._referees: list[ToolDeleteReferee] = []

    def register(self, provider: ToolDeleteReferee) -> None:
        """Register ``provider`` as a delete referee, raising on a double registration."""
        if provider in self._referees:
            raise ValueError("this delete referee is already registered")
        self._referees.append(provider)

    def all(self) -> list[ToolDeleteReferee]:
        """Every registered delete referee, in registration order."""
        return list(self._referees)

    def reset(self) -> None:
        """Clear every registered referee (called on each ``start()``)."""
        self._referees.clear()
