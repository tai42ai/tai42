"""Errors a mid-form reaction raises through the ``react`` facet.

One family so a consumer catches :class:`FormReactionError` and reaches every reaction failure.
Each error's ``reason`` is the sentence the reaction door shows the person, and the ``{reason}``
a form's ``update_failed`` sentence takes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# The most characters a reaction error's ``reason`` holds. A surface that fills a
# ``{reason}`` token into a vendor-capped text checks its template against this worst case
# at send. The reason can carry handler-supplied text (field names, a handler's error map),
# so this is an abuse ceiling: a real reason is far shorter.
FORM_REACTION_REASON_MAX_CHARS = 500


class FormReactionError(Exception):
    """A mid-form reaction that could not complete.

    ``reason`` is the sentence the reaction door shows the person (a form's ``update_failed``
    ``{reason}``), at most :data:`FORM_REACTION_REASON_MAX_CHARS` characters. A longer sentence
    is logged whole at WARNING, kept whole as the exception message, and shortened in ``reason``
    to the bound with a trailing ellipsis.
    """

    def __init__(self, reason: str) -> None:
        """Carry ``reason``, the door's sentence (bounded), and use the whole sentence as the exception message."""
        if len(reason) > FORM_REACTION_REASON_MAX_CHARS:
            logger.warning(
                "%s reason is %d characters, over the %d-character bound; the person sees it shortened: %s",
                type(self).__name__,
                len(reason),
                FORM_REACTION_REASON_MAX_CHARS,
                reason,
            )
            self.reason = reason[: FORM_REACTION_REASON_MAX_CHARS - 1] + "…"
        else:
            self.reason = reason
        super().__init__(reason)


class FormReactionClosedError(FormReactionError):
    """The interaction is not an OPEN reacting form (missing, answered, or not reacting)."""


class FormReactionRequestError(FormReactionError):
    """The caller's reaction request is malformed (an undeclared event trigger, bad partial values)."""


class FormReactionHandlerError(FormReactionError):
    """The reaction handler raised, missed its deadline, or returned an invalid form update."""
