"""The redis-backend guard shared by every routing operation, plus the lazy store accessors
and the read guards (route existence, person route set, uniform thread-not-found)."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from tai42_contract.conversations import ConversationRoute, Person

from tai42_skeleton.conversations.managers.base_conversations_manager import BaseConversationsManager
from tai42_skeleton.conversations.managers.in_memory_conversations_manager import InMemoryConversationsManager
from tai42_skeleton.operations import NotFoundError
from tai42_skeleton.operations.errors import NotSupportedError

if TYPE_CHECKING:
    from tai42_skeleton.conversations.mode import ConversationModeStore
    from tai42_skeleton.conversations.persons import ConversationPersonStore
    from tai42_skeleton.conversations.records import ConversationRecordStore

# Surfaced before a create does any bind work it would then have to discard.
_NO_BACKEND = "conversation routes require the redis conversations backend"


# The ``operations.conversations`` package instance THIS submodule belongs to, captured from
# ``sys.modules`` at import. A registry reload pops and re-imports the whole package, so each
# generation's submodules bind to their OWN package object here — a stale-but-orphaned handler
# then still reads (and a test still patches) the same generation it was built with. This is the
# package-alias test-double seam for ``get_conversations_manager``/``resolve_caller``/
# ``assert_execution_key_bindable``/``_person_store``.
_pkg = sys.modules["tai42_skeleton.operations.conversations"]


def _require_backend() -> BaseConversationsManager:
    manager = _pkg.get_conversations_manager()
    if isinstance(manager, InMemoryConversationsManager):
        raise NotSupportedError(_NO_BACKEND)
    return manager


def _person_store() -> ConversationPersonStore:
    """The person store over the live conversations settings. Called only after
    :func:`_require_backend`, so its own backend guard never fires here."""
    from tai42_skeleton.conversations.persons import ConversationPersonStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    return ConversationPersonStore(ConversationsSettings())


def _record_store() -> ConversationRecordStore:
    """The answer/record store over the live conversations settings. Called only after
    :func:`_require_backend`, so its own backend guard never fires here."""
    from tai42_skeleton.conversations.records import ConversationRecordStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    return ConversationRecordStore(ConversationsSettings())


def _mode_store() -> ConversationModeStore:
    """The per-thread mode-override store over the live conversations settings. Called only
    after :func:`_require_backend`, so its own backend guard never fires here."""
    from tai42_skeleton.conversations.mode import ConversationModeStore
    from tai42_skeleton.conversations.settings import ConversationsSettings

    return ConversationModeStore(ConversationsSettings())


def _person_routes(person: Person) -> set[str]:
    """Every route name the person has written under, straight off its address rows — the
    routes whose indexes the aggregated transcript spans and the authz check reads."""
    return {route for address in person.addresses for route in address.routes}


async def _require_route(manager: BaseConversationsManager, route_name: str) -> ConversationRoute:
    """The route a thread read is against. Refuses a read on a route that does not exist, so
    an unknown route is a loud 404 and not an empty listing. Called only once the reader is
    authorized: existence is a fact the answer discloses."""
    route = await manager.get_route(route_name)
    if route is None:
        raise NotFoundError(f"conversation route not found: {route_name!r}")
    return route


def _thread_not_found(thread_id: str) -> NotFoundError:
    """The uniform not-found the thread reads give for a thread that is absent, expired under
    the retention TTL, or keyed to another route."""
    return NotFoundError(f"conversation thread not found: {thread_id!r}")
