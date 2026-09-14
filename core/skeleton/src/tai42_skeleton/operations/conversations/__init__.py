"""Conversation-route management operations — the routing-table surface behind the
``/api/conversations*`` doors, the ``tai conversations`` CLI and the MCP tools.

A route binds an inbound door (``api`` or ``channel``) to a target — an ``agent`` run or a
``tool`` dispatch — and the ``execution_key`` that turn runs AS. A row's ``callback_secret``
is shown ONCE at create and withheld from every read. ``create_conversation_route``
DELEGATES authority, so it carries the ``authority_changing`` tier and binds the key BEFORE
any write.

Every routing operation requires the redis conversations backend and otherwise refuses
with a loud 501 (``NotSupportedError``). This package's ``__init__`` is the public seam:
the door submodules stay internal and the whole surface is reached as
``tai42_skeleton.operations.conversations.<door>``.
"""

from __future__ import annotations

# Package-alias test-double seam: these symbols are patched at this package alias by tests, so
# they are bound as package attributes BEFORE the door submodules import and every door reads
# them THROUGH this package object at call time.
from tai42_skeleton.conversations.cache import get_conversations_manager as get_conversations_manager
from tai42_skeleton.operations._authority import assert_execution_key_bindable as assert_execution_key_bindable
from tai42_skeleton.operations._authority import resolve_caller as resolve_caller

from .backend import _person_store as _person_store
from .mode import get_conversation_thread_mode, set_conversation_thread_mode
from .models import MAX_THREAD_PAGE, MAX_THREAD_PAGE_SIZE
from .operator_send import send_conversation_thread_message
from .persons import get_conversation_person, set_conversation_person_locale
from .routes import (
    _unclaimed_channel_identity as _unclaimed_channel_identity,
)
from .routes import (
    create_conversation_route,
    delete_conversation_route,
    get_conversation_message,
    get_conversation_route,
    list_conversation_routes,
)
from .target_config import (
    delete_conversation_config,
    get_conversation_config,
    list_conversation_configs,
    set_conversation_config,
)
from .threads_delete import delete_conversation_person, delete_conversation_thread
from .threads_read import (
    get_conversation_thread,
    list_conversation_threads,
    list_failed_conversations,
    search_conversation_messages,
)

__all__ = [
    "MAX_THREAD_PAGE",
    "MAX_THREAD_PAGE_SIZE",
    "create_conversation_route",
    "delete_conversation_config",
    "delete_conversation_person",
    "delete_conversation_route",
    "delete_conversation_thread",
    "get_conversation_config",
    "get_conversation_message",
    "get_conversation_person",
    "get_conversation_route",
    "get_conversation_thread",
    "get_conversation_thread_mode",
    "list_conversation_configs",
    "list_conversation_routes",
    "list_conversation_threads",
    "list_failed_conversations",
    "search_conversation_messages",
    "send_conversation_thread_message",
    "set_conversation_config",
    "set_conversation_person_locale",
    "set_conversation_thread_mode",
]
