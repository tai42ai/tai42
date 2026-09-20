"""The turn engine — turns an accepted message into an agent or tool turn.

One inbound message (channel door :func:`accept`, authed API door
:func:`submit_api_message`, event door :func:`submit_event`) resolves to its route, runs
that route's target IN-PROCESS under the route's execution key, and persists the produced
answer as a durable record the delivery executor sends back.

A route targets a TOOL or an AGENT, and the two kinds deliver a PARKED target's resumed
answer by different paths. A parked tool target resumes out of band and its resuming
consumer owns delivery-back, so the platform binds a generic tool-route completion and ends
the turn silently. A parked agent target has no self-delivery, so the platform binds the
completion tool for the run and posts the resumed answer back into this thread.

This package re-exports the public turn API; the concern submodules hold the doors, the
target runs, the pairing turn, scheduling and the resumed-answer delivery tools.
"""

from __future__ import annotations

from tai42_skeleton.conversations.turn.api_door import submit_api_message
from tai42_skeleton.conversations.turn.api_wait import ApiSubmitResult
from tai42_skeleton.conversations.turn.completion_delivery import (
    COMPLETION_TOOL_NAME,
    DELIVER_TOOL_COMPLETION_NAME,
    deliver_agent_completion,
    deliver_tool_completion,
)
from tai42_skeleton.conversations.turn.errors import (
    CompletionDeliveryError,
    ConversationRouteResolutionError,
    EventTargetNotToolError,
    OperatorAppendError,
    ThreadNotFoundError,
    UnauthenticatedApiCallerError,
)
from tai42_skeleton.conversations.turn.event_door import submit_event
from tai42_skeleton.conversations.turn.intake import accept
from tai42_skeleton.conversations.turn.operator_send import operator_send
from tai42_skeleton.conversations.turn.redrive import redrive_accepted

# Re-exported (not part of the public API) so the command classifier's ``mint_pairing_code``
# can read it through this package at call time, breaking the turn<->pairing import cycle
# while staying patchable at the ``conversations.turn`` alias.
from tai42_skeleton.conversations.turn.routing import _resolve_channel_route  # noqa: F401

__all__ = [
    "COMPLETION_TOOL_NAME",
    "DELIVER_TOOL_COMPLETION_NAME",
    "ApiSubmitResult",
    "CompletionDeliveryError",
    "ConversationRouteResolutionError",
    "EventTargetNotToolError",
    "OperatorAppendError",
    "ThreadNotFoundError",
    "UnauthenticatedApiCallerError",
    "accept",
    "deliver_agent_completion",
    "deliver_tool_completion",
    "operator_send",
    "redrive_accepted",
    "submit_api_message",
    "submit_event",
]
