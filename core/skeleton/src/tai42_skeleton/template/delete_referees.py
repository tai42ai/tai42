"""The template-delete referee registry, the body behind ``app.storage.register_template_delete_referee``.

A referee is called with the set of stored ids a delete would remove and answers one
``ReferenceHolder`` per place a stored object it owns names one of them; it changes nothing.
:meth:`TemplateDeleteRefereeRegistry.holders_of` is the gather the delete check reads.
Reset on every ``start()``: the platform's referees re-register from their startup and reload
handlers and a plugin's from its module import. A second registration of one referee raises.
"""

from __future__ import annotations

from tai42_contract.tools import ReferenceHolder, TemplateDeleteReferee

from tai42_skeleton.template.referee_answers import checked_holders


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

    async def holders_of(self, ids: frozenset[str]) -> list[ReferenceHolder]:
        """Every holder of one of ``ids``: each referee's answer, in registration order.

        An answer that is not a list of ``ReferenceHolder`` is the referee's bug and raises
        ``TypeError`` naming the referee.
        """
        holders: list[ReferenceHolder] = []
        for referee in list(self._referees):
            holders.extend(checked_holders(referee, await referee(ids)))
        return holders

    def reset(self) -> None:
        """Clear every registered referee (called on each ``start()``)."""
        self._referees.clear()


__all__ = ["TemplateDeleteRefereeRegistry"]
