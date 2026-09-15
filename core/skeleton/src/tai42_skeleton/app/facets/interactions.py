"""The ``app.interactions`` facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import _Facet

if TYPE_CHECKING:
    from tai42_contract.interactions import AskUser


class InteractionsFacet(_Facet):
    """``app.interactions`` — the ``ask_user`` facade (``AppInteractions``)."""

    @property
    def ask_user(self) -> AskUser:
        """The bound, ``AskUser``-typed ``ask_user`` callable for an in-process plugin.

        Lets a plugin ask a human without importing the skeleton. A facade EXPOSURE of the
        existing helper — its rich signature and return contract are forwarded verbatim, no
        new ask semantics.
        """
        from tai42_skeleton.interactions.helper import ask_user

        return ask_user
