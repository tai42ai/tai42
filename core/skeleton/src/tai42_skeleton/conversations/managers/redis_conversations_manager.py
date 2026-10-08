"""The Redis-backed conversation-route store, with atomic put/delete Lua scripts."""

import logging
from types import MappingProxyType
from typing import Any

from tai42_contract.conversations import ConversationRoute
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations.managers.base_conversations_manager import (
    BaseConversationsManager,
    DoorFlipRefusedError,
)
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.store_snapshot import StoreSnapshot, new_write_token
from tai42_skeleton.utils.redis_typing import awaited, eval_script

logger = logging.getLogger(__name__)

# The per-route key write and the name-index add must be ONE atomic unit, or a create
# racing a delete of the same name leaves the row keyed but unindexed, or indexed but
# keyless. The route's THREAD index (keyspace 8) is read in that same unit: the door-flip
# refusal is only a guard if the count it decides on cannot move before the write it
# guards. The table's write token is set in the same unit, so a reader's snapshot can never
# be tagged with a token whose write it has not seen. Every key is passed in from
# ``ConversationsSettings``.
#
# put: KEYS[1]=names index, KEYS[2]=the route's own key, KEYS[3]=the route's thread index,
# KEYS[4]=the table's version key; ARGV = route_name, route_json, door, token. Returns 1 when
# the row already existed (a replace), 0 when it is newly created, or ``{held, stored_door}``
# — writing nothing, token included — when the write would flip the door of a route that
# still holds threads.
_PUT_LUA = """
-- conversations:route:put:atomic
local names_key, route_key, threads_key, version_key = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local route_name, route_json, door, token = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
local existing = redis.call('GET', route_key)
if existing then
  local stored_door = cjson.decode(existing)['door']
  local held = redis.call('ZCARD', threads_key)
  if stored_door ~= door and held > 0 then return {held, stored_door} end
end
redis.call('SET', route_key, route_json)
redis.call('SADD', names_key, route_name)
redis.call('SET', version_key, token)
if existing then return 1 end
return 0
"""

# delete: KEYS[1]=names index, KEYS[2]=the route's own key, KEYS[3]=the table's version key;
# ARGV = route_name, token. Returns 1 when a row was removed, 0 when none existed; the token is
# set either way.
_DELETE_LUA = """
-- conversations:route:delete:atomic
local names_key, route_key, version_key = KEYS[1], KEYS[2], KEYS[3]
local route_name, token = ARGV[1], ARGV[2]
local removed = redis.call('DEL', route_key)
redis.call('SREM', names_key, route_name)
redis.call('SET', version_key, token)
return removed
"""


def _as_str(value: Any) -> str:
    """Normalize Redis's ``bytes``-or-``str`` return to ``str``."""
    return value.decode() if isinstance(value, bytes) else value


class RedisConversationsManager(BaseConversationsManager):
    """A Redis-backed conversation-route store over :class:`BaseConversationsManager`.

    Reads are served from a per-process snapshot of the route table, re-loaded whenever the
    table's write token (set by every write script) differs from the one the snapshot was
    loaded under, so a write through any process is seen on every process's next read. The
    keys under the conversations prefix are owned by these scripts: a row written by hand
    outside them is not seen until the next platform write sets a token.
    """

    def __init__(self, settings: ConversationsSettings) -> None:
        """Bind ``settings``; no route snapshot is held until the first read."""
        super().__init__(settings)
        self._route_snapshot: StoreSnapshot[str, ConversationRoute] | None = None

    @property
    def durable(self) -> bool:
        """``True``: every conversations store persists in the conversations Redis."""
        return True

    async def put_route(self, route: ConversationRoute) -> bool:
        """Store ``route`` atomically; return whether it was newly created (vs a replace).

        Raises :class:`DoorFlipRefusedError` when the write would flip the door of a route
        that still holds threads.
        """
        async with client_ctx(RedisClient, self.settings.redis) as r:
            existed = await eval_script(
                r,
                _PUT_LUA,
                4,
                self.settings.route_names_key,
                self.settings.route_key(route.route_name),
                self.settings.route_threads_key(route.route_name),
                self.settings.route_version_key,
                route.route_name,
                route.model_dump_json(),
                route.door,
                new_write_token(),
            )
        # A list reply is the refusal: ``{held, stored_door}``, and nothing was written.
        if isinstance(existed, list):
            held, stored_door = existed
            raise DoorFlipRefusedError(route.route_name, _as_str(stored_door), route.door, int(held))
        # ``existed`` truthy ⇒ a replace; falsy ⇒ a fresh create.
        return not bool(existed)

    async def get_route(self, route_name: str) -> ConversationRoute | None:
        """The route named ``route_name``, or ``None`` when none is stored.

        An index member whose row was unreadable at load is read live, so a missing row answers
        ``None`` and an unparseable one raises, as a single-key read does.
        """
        snapshot = await self._load_routes()
        if route_name in snapshot.unreadable:
            async with client_ctx(RedisClient, self.settings.redis) as r:
                raw = await awaited(r.get(self.settings.route_key(route_name)))
            if raw is None:
                return None
            return ConversationRoute.model_validate_json(_as_str(raw))
        return snapshot.rows.get(route_name)

    async def delete_route(self, route_name: str) -> bool:
        """Delete the route named ``route_name`` atomically; return whether a row was removed."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            removed = await eval_script(
                r,
                _DELETE_LUA,
                3,
                self.settings.route_names_key,
                self.settings.route_key(route_name),
                self.settings.route_version_key,
                route_name,
                new_write_token(),
            )
        return bool(removed)

    async def list_routes(self) -> tuple[dict[str, ConversationRoute], int]:
        """Every stored route keyed by route name, with a count of unreadable ones.

        ``unreadable`` counts the indexed names whose row was gone or unparseable, so a shorter
        map is a truthful count and never a silent cut.
        """
        snapshot = await self._load_routes()
        return dict(snapshot.rows), len(snapshot.unreadable)

    async def _load_routes(self) -> StoreSnapshot[str, ConversationRoute]:
        """The route snapshot current for the table's write token, re-loaded when the token moved.

        The token is read FIRST: a write landing between it and the row reads leaves the new
        snapshot tagged with the older token, so the next read loads again.
        """
        async with client_ctx(RedisClient, self.settings.redis) as r:
            token = await awaited(r.get(self.settings.route_version_key))
            token = None if token is None else _as_str(token)
            held = self._route_snapshot
            if held is not None and held.token == token:
                return held
            names = await awaited(r.smembers(self.settings.route_names_key))
            name_list = sorted(_as_str(name) for name in names)
            raws = await awaited(r.mget([self.settings.route_key(name) for name in name_list])) if name_list else []
        routes: dict[str, ConversationRoute] = {}
        unreadable: set[str] = set()
        for name, raw in zip(name_list, raws, strict=True):
            if raw is None:
                # Indexed name with no row: a corrupt state (the row key never expires).
                logger.warning("conversations: route name %r is indexed but has no row; skipping", name)
                unreadable.add(name)
                continue
            try:
                routes[name] = ConversationRoute.model_validate_json(_as_str(raw))
            except ValueError:
                logger.warning("conversations: route name %r has an unparseable row and was skipped", name)
                unreadable.add(name)
                continue
        snapshot = StoreSnapshot(token=token, rows=MappingProxyType(routes), unreadable=frozenset(unreadable))
        self._route_snapshot = snapshot
        return snapshot
