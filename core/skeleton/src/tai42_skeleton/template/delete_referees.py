"""The template-delete referee registry, the body behind ``app.storage.register_template_delete_referee``.

A referee is called with the set of stored ids a delete would remove and answers one
``ReferenceHolder`` per place a stored object it owns names one of them; it changes nothing.
Reset on every ``start()``: the platform's referees re-register from their startup and reload
handlers and a plugin's from its module import. A second registration of one referee raises.
"""

from __future__ import annotations

from tai42_contract.tools import TemplateDeleteReferee


class TemplateDeleteRefereeRegistry:
    """The process-wide registry of template-delete referees."""

    def __init__(self) -> None:
        """Start with no registered referees."""
        self._referees: list[TemplateDeleteReferee] = []

    def register(self, referee: TemplateDeleteReferee) -> None:
        """Register ``referee``, raising on a double registration."""
        if referee in self._referees:
            raise ValueError("this template delete referee is already registered")
        self._referees.append(referee)

    def all(self) -> list[TemplateDeleteReferee]:
        """Every registered referee, in registration order."""
        return list(self._referees)

    def reset(self) -> None:
        """Clear every registered referee (called on each ``start()``)."""
        self._referees.clear()


__all__ = ["TemplateDeleteRefereeRegistry"]
