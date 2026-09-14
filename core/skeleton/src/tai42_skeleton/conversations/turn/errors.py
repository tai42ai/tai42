"""The errors the conversation doors raise when an inbound turn cannot be admitted."""

from __future__ import annotations

from tai42_skeleton.operations.errors import NotSupportedError


class ConversationRouteResolutionError(LookupError):
    """No route matches the inbound message, so it is refused rather than dropped."""


class UnauthenticatedApiCallerError(NotSupportedError):
    """The API door was reached with no authenticated caller principal. The turn is refused:
    every thread and rate bucket on that door is keyed by its caller, and an anonymous one
    would be shared by everybody."""


class ThreadNotFoundError(LookupError):
    """The event door named a thread that does not exist on the route. An event enters an
    EXISTING thread as a turn and never mints one, so an unknown (route, address) or thread
    id is refused rather than opening a new conversation."""


class EventTargetNotToolError(NotSupportedError):
    """The event door named a route whose target is an AGENT. An event carries a structured
    payload and no rendered text, so there is nothing to hand an agent turn; only a tool
    target (which maps the payload through its ``payload_expr``) may run an event."""


class OperatorAppendError(RuntimeError):
    """Appending an operator's message to the thread's agent checkpoint failed, so the send
    is refused and NO record is created — the operator's reply must not stand in the
    transcript while it is absent from the memory a later agent turn reads."""


class CompletionDeliveryError(RuntimeError):
    """The resumed answer of an async-parked agent turn could not be delivered — its thread
    could not be reversed to a live route + address. Raised loudly so the completion
    continuation's at-least-once seam retains and retries rather than dropping the answer."""
