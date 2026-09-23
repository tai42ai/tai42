"""The caller-ask predicate and the refusal the user-facing doors share.

A ``to="caller"`` ask is addressed to the calling run: it is resolved only by that
run resuming (its answer is handed back and subject-tracked), never by a person.
Every user-facing read surface hides such an ask and every user-facing resolution
door refuses it. The predicate and the refusal message live here once; each door
applies them in its own idiom (a raised ``ConflictError``, a 409 JSON body).
"""

from __future__ import annotations

from tai42_contract.interactions import InteractionState

#: The refusal a user-facing resolution door surfaces when handed a caller ask.
#: States the rule itself: a caller ask is resumed only by its calling run, never
#: answered by a person.
CALLER_ASK_RESOLUTION_REFUSED = (
    "this ask is addressed to the calling run and is resolved only by that run resuming; a person can never answer it"
)


def is_caller_ask(state: InteractionState) -> bool:
    """Whether ``state`` is an ask addressed to the calling run (``to="caller"``)."""
    return state.request.to == "caller"
