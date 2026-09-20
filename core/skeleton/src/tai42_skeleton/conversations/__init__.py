"""Conversation bridge — client messages routed to an agent turn whose answer is durably stored and delivered back.

The models and the ``AppConversations`` facet are the contract; this package is the host
implementation: ``managers`` the routing-row store
(keyspace 4), ``cache`` its singleton accessor, ``backup`` the row export/import seam,
``ledger`` the channel send progress (keyspace 5), ``settings`` the ``CONVERSATIONS_*``
config carrying the keyspace helpers and every bound.

Without ``CONVERSATIONS_REDIS_URL`` there is no durable backend and every routing operation
refuses with a loud 501.
"""

from typing import TYPE_CHECKING, Any

from tai42_skeleton.conversations.cache import get_conversations_manager
from tai42_skeleton.conversations.settings import ConversationsSettings

if TYPE_CHECKING:
    from tai42_skeleton.conversations.delivery import record_delivery_status, redrive_pending
    from tai42_skeleton.conversations.delivery_sweep import start_delivery_sweep, stop_delivery_sweep
    from tai42_skeleton.conversations.pending import pending_messages
    from tai42_skeleton.conversations.turn.api_door import submit_api_message
    from tai42_skeleton.conversations.turn.event_door import submit_event
    from tai42_skeleton.conversations.turn.intake import accept
    from tai42_skeleton.conversations.turn.redrive import redrive_accepted

# Lazy so importing a lightweight submodule (settings, the routing manager) does not drag
# in the agent contract, the execution-identity authorizer and the HTTP client.
_LAZY: dict[str, tuple[str, str]] = {
    "accept": ("turn.intake", "accept"),
    "submit_api_message": ("turn.api_door", "submit_api_message"),
    "submit_event": ("turn.event_door", "submit_event"),
    "record_delivery_status": ("delivery", "record_delivery_status"),
    "pending_messages": ("pending", "pending_messages"),
    "redrive_pending": ("delivery", "redrive_pending"),
    "redrive_accepted": ("turn.redrive", "redrive_accepted"),
    "start_delivery_sweep": ("delivery_sweep", "start_delivery_sweep"),
    "stop_delivery_sweep": ("delivery_sweep", "stop_delivery_sweep"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f"{__name__}.{target[0]}")
    return getattr(module, target[1])


__all__ = [
    "ConversationsSettings",
    "accept",
    "get_conversations_manager",
    "pending_messages",
    "record_delivery_status",
    "redrive_accepted",
    "redrive_pending",
    "start_delivery_sweep",
    "stop_delivery_sweep",
    "submit_api_message",
    "submit_event",
]
