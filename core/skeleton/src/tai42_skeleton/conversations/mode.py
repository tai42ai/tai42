"""The per-thread conversation-mode store — keyspace 18 of the conversation bridge: the
``thread_id`` → ``agent``/``manual`` override map, transient runtime state (NOT a backup
section) and Redis-backed.

An absent override means the route's ``initial_mode`` stands, so :func:`effective_mode`
reads the override and falls back to the route default. Only ``agent`` and ``manual`` are
storable values; anything else raises loudly, at the write and at the read, rather than being
coerced or silently ignored.

LIFETIME: the override never outlives the conversation. It is written with the same
``answer_retention_ttl_seconds`` clock the answer records carry (:meth:`set_mode`), and every
new activity on the thread — a channel/api accept intake and an operator send — extends that
window (:meth:`refresh_ttl`, a single ``EXPIRE`` that NEVER recreates a missing key, so
absence stays the route default). A thread that goes quiet past the retention window loses
its override with its records, and a returning address starts at the route default again. An
explicit operator thread delete (:meth:`ConversationRecordStore.drop_thread`) or route delete
(:meth:`ConversationRecordStore.drop_route_threads`) reclaims the key at once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_contract.agent import Agent
from tai42_contract.conversations import CONVERSATION_MODES, ConversationMode, ConversationRoute
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.agent.thread_reservation import PERSON_THREAD_PREFIX
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations.errors import NotSupportedError
from tai42_skeleton.utils.redis_typing import awaited

if TYPE_CHECKING:
    from collections.abc import Iterable

    from tai42_skeleton.conversations.managers.base_conversations_manager import BaseConversationsManager

_NO_BACKEND = "conversation thread mode requires the redis conversations backend"


def _validate_mode(mode: str) -> ConversationMode:
    """Return ``mode`` as a :data:`ConversationMode`, or raise on any other value — a mode
    is never coerced or defaulted."""
    if mode not in CONVERSATION_MODES:
        raise ValueError(f"conversation mode must be one of {list(CONVERSATION_MODES)}, got {mode!r}")
    return mode  # pyright: ignore[reportReturnType]


class ConversationModeStore:
    """The Redis-backed per-thread mode-override store (keyspace 18). Construction refuses
    with a loud 501 without the redis conversations backend."""

    def __init__(self, settings: ConversationsSettings) -> None:
        if settings.in_memory:
            raise NotSupportedError(_NO_BACKEND)
        self.settings = settings

    async def get_mode(self, thread_id: str) -> ConversationMode | None:
        """The stored override for ``thread_id``, or ``None`` when none is set. A stored
        value that is not a known mode is corruption and raises."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            raw = await awaited(r.get(self.settings.mode_key(thread_id)))
        if raw is None:
            return None
        value = raw.decode() if isinstance(raw, bytes) else raw
        return _validate_mode(value)

    async def set_mode(self, thread_id: str, mode: str) -> ConversationMode:
        """Set ``thread_id``'s override to ``mode`` (validated), returning the stored value.
        The override carries the record retention TTL, so it never outlives the conversation;
        thread activity extends it through :meth:`refresh_ttl`."""
        checked = _validate_mode(mode)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            await awaited(
                r.set(self.settings.mode_key(thread_id), checked, ex=self.settings.answer_retention_ttl_seconds)
            )
        return checked

    async def refresh_ttl(self, thread_id: str) -> bool:
        """Extend a live override's TTL to the retention window on new thread activity,
        returning whether an override was present to extend. A no-op when none is set —
        ``EXPIRE`` never resurrects a missing key, so absence stays the route default."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return bool(
                await awaited(r.expire(self.settings.mode_key(thread_id), self.settings.answer_retention_ttl_seconds))
            )

    async def delete_mode(self, thread_id: str) -> bool:
        """Remove ``thread_id``'s override, returning whether one was removed."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return bool(await awaited(r.delete(self.settings.mode_key(thread_id))))


async def effective_mode(route: ConversationRoute, thread_id: str) -> ConversationMode:
    """The mode in force for ``thread_id`` on ``route``: the per-thread override if set,
    else the no-override default from :func:`default_mode`."""
    override = await ConversationModeStore(ConversationsSettings()).get_mode(thread_id)
    return override if override is not None else await default_mode(route, thread_id)


async def default_mode(route: ConversationRoute, thread_id: str) -> ConversationMode:
    """The no-override default for ``thread_id``. A route-keyed thread takes its route's
    ``initial_mode``. A LINKED person's aggregated thread (``bridge:@person:`` prefix) spans
    several routes and one operator control decision covers them all: it defaults to
    ``manual`` when ANY route the person's addresses span defaults to ``manual``, so a linked
    person is never silently agent-served on a channel an operator meant to hold by hand,
    else ``agent``. A person row that has vanished falls back to the named route's default."""
    if not thread_id.startswith(PERSON_THREAD_PREFIX):
        return route.initial_mode
    from tai42_skeleton.conversations.cache import get_conversations_manager
    from tai42_skeleton.conversations.persons import ConversationPersonStore

    person = await ConversationPersonStore(ConversationsSettings()).get_by_id(thread_id[len(PERSON_THREAD_PREFIX) :])
    if person is None:
        return route.initial_mode
    route_names = {name for address in person.addresses for name in address.routes}
    return await default_mode_for_routes(get_conversations_manager(), route_names)


async def default_mode_for_routes(manager: BaseConversationsManager, route_names: Iterable[str]) -> ConversationMode:
    """``manual`` when ANY of ``route_names`` resolves to a route defaulting to ``manual``,
    else ``agent`` — the one place the several-routes fold lives. A name that no longer
    resolves imposes nothing, so a deleted route never forces manual."""
    for name in route_names:
        spanned = await manager.get_route(name)
        if spanned is not None and spanned.initial_mode == "manual":
            return "manual"
    return "agent"


def supports_thread_append(agent: Agent) -> bool:
    """Whether ``agent`` implements :meth:`Agent.append_thread_messages` rather than leaving
    it the ABC default. A memoryless target (the default) has no thread memory to feed, so a
    manual-mode inbound and an operator send skip the append and record silently; a
    memory-holding target has the message appended for a later agent turn to read."""
    return type(agent).append_thread_messages is not Agent.append_thread_messages


class NoBridgeTurnError(RuntimeError):
    """``set_conversation_mode`` was called with no bridge turn in flight, so there is no
    current conversation whose mode to set — a builtin used outside an agent turn refuses
    loudly rather than acting on nothing. Programmatic callers set a thread's mode through the
    ``PUT /thread/mode`` door instead, which names its thread explicitly."""


async def set_current_thread_mode(mode: str) -> ConversationMode:
    """Set the CURRENT bridge turn's thread mode to ``mode``, returning the stored value — the
    feature body behind the ``set_conversation_mode`` builtin tool.

    Reads the turn-scoped bridge context an agent turn establishes and keys the mode override
    on its ``thread_id`` (the same thread the turn ran on, and the next one reads). Called
    outside a bridge turn (no context) it raises :class:`NoBridgeTurnError`; a ``mode`` outside
    the vocabulary is a loud ``ValueError``.
    """
    checked = _validate_mode(mode)
    from tai42_skeleton.conversations.turn_context import current_bridge_turn

    context = current_bridge_turn()
    if context is None:
        raise NoBridgeTurnError(
            "set_conversation_mode was called outside a bridge turn; there is no current conversation whose "
            "mode to set. Set a thread's mode through the PUT /thread/mode door instead."
        )
    return await ConversationModeStore(ConversationsSettings()).set_mode(context.thread_id, checked)


__all__ = [
    "ConversationModeStore",
    "NoBridgeTurnError",
    "default_mode",
    "default_mode_for_routes",
    "effective_mode",
    "set_current_thread_mode",
    "supports_thread_append",
]
