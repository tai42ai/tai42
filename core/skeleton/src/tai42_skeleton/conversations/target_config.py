"""The per-target conversation config store — keyspace 9 of the conversation bridge.

The durable, backed-up ``(target_kind, target_name)`` →
:class:`TargetConversationConfig` map (``multichannel`` opt-in + first-contact
``greeting_template``).

Redis-backed and gated exactly as the routing-row store: construction refuses with a loud
501 without the redis conversations backend, because a durable operator-config map cannot
live per-process. The accept path and the pairing tool read a target's config through
:meth:`ConversationTargetConfigStore.get`, and the backup section exports the whole map
through :meth:`~ConversationTargetConfigStore.list`.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any

from tai42_contract.conversations import TargetConversationConfig
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.store_snapshot import StoreSnapshot, new_write_token
from tai42_skeleton.operations.errors import NotSupportedError
from tai42_skeleton.utils.redis_typing import awaited, eval_script

logger = logging.getLogger(__name__)

_NO_BACKEND = "conversation target config requires the redis conversations backend"

# The per-config key write and the name-index add must be ONE atomic unit, or an upsert
# racing a delete of the same key leaves the row keyed but unindexed, or indexed but
# keyless. The map's write token is set in the same unit. Every key is passed in from
# ``ConversationsSettings``.
#
# put: KEYS[1]=names index, KEYS[2]=the config's own key, KEYS[3]=the map's version key;
# ARGV = member, config_json, token. Returns 1 when the row already existed (a replace), 0
# when it is newly created.
_PUT_LUA = """
-- conversations:config:put:atomic
local names_key, config_key, version_key = KEYS[1], KEYS[2], KEYS[3]
local member, config_json, token = ARGV[1], ARGV[2], ARGV[3]
local existed = redis.call('EXISTS', config_key)
redis.call('SET', config_key, config_json)
redis.call('SADD', names_key, member)
redis.call('SET', version_key, token)
return existed
"""

# delete: KEYS[1]=names index, KEYS[2]=the config's own key, KEYS[3]=the map's version key;
# ARGV = member, token. Returns 1 when a row was removed, 0 when none existed; the token is
# set either way.
_DELETE_LUA = """
-- conversations:config:delete:atomic
local names_key, config_key, version_key = KEYS[1], KEYS[2], KEYS[3]
local member, token = ARGV[1], ARGV[2]
local removed = redis.call('DEL', config_key)
redis.call('SREM', names_key, member)
redis.call('SET', version_key, token)
return removed
"""


def _as_str(value: Any) -> str:
    """Normalize Redis's ``bytes``-or-``str`` return to ``str``."""
    return value.decode() if isinstance(value, bytes) else value


def _member(target_kind: str, target_name: str) -> str:
    """The index member for a config key — the row-key suffix.

    Appending it to :attr:`ConversationsSettings.target_config_key_prefix` rebuilds the
    row key it names.
    """
    return f"{target_kind}:{target_name}"


class ConversationTargetConfigStore:
    """The Redis-backed per-target config store (keyspace 9).

    Construction refuses with a loud 501 without the redis conversations backend. Reads are
    served from a per-process snapshot of the map, re-loaded whenever the map's write token (set
    by every write script) differs from the one the snapshot was loaded under. The keys under the
    conversations prefix are owned by these scripts: a row written by hand outside them is not
    seen until the next platform write sets a token.
    """

    def __init__(self, settings: ConversationsSettings) -> None:
        """Store ``settings``; refuse with a loud 501 without the redis conversations backend."""
        if settings.in_memory:
            raise NotSupportedError(_NO_BACKEND)
        self.settings = settings
        self._snapshot: StoreSnapshot[tuple[str, str], TargetConversationConfig] | None = None

    async def upsert(self, config: TargetConversationConfig) -> bool:
        """Store ``config`` (an upsert — create or replace), keeping the name index in lockstep.

        Return ``True`` when the row is newly created, ``False`` when it replaced an
        existing row of the same ``(target_kind, target_name)`` key.
        """
        async with client_ctx(RedisClient, self.settings.redis) as r:
            existed = await eval_script(
                r,
                _PUT_LUA,
                3,
                self.settings.target_config_names_key,
                self.settings.target_config_key(config.target_kind, config.target_name),
                self.settings.target_config_version_key,
                _member(config.target_kind, config.target_name),
                config.model_dump_json(),
                new_write_token(),
            )
        # ``existed`` truthy ⇒ a replace; falsy ⇒ a fresh create.
        return not bool(existed)

    async def get(self, target_kind: str, target_name: str) -> TargetConversationConfig | None:
        """The stored config for ``(target_kind, target_name)``, or ``None`` when none exists.

        An index member whose row was unreadable at load is read live, so a missing row answers
        ``None`` and an unparseable one raises, as a single-key read does.
        """
        snapshot = await self._load_configs()
        if _member(target_kind, target_name) in snapshot.unreadable:
            async with client_ctx(RedisClient, self.settings.redis) as r:
                raw = await awaited(r.get(self.settings.target_config_key(target_kind, target_name)))
            if raw is None:
                return None
            return TargetConversationConfig.model_validate_json(_as_str(raw))
        return snapshot.rows.get((target_kind, target_name))

    async def delete(self, target_kind: str, target_name: str) -> bool:
        """Remove the config for ``(target_kind, target_name)``, keeping the name index in lockstep.

        Return ``True`` when a row was removed, ``False`` when none existed.
        """
        async with client_ctx(RedisClient, self.settings.redis) as r:
            removed = await eval_script(
                r,
                _DELETE_LUA,
                3,
                self.settings.target_config_names_key,
                self.settings.target_config_key(target_kind, target_name),
                self.settings.target_config_version_key,
                _member(target_kind, target_name),
                new_write_token(),
            )
        return bool(removed)

    async def list(self) -> tuple[dict[tuple[str, str], TargetConversationConfig], int]:
        """Every stored config keyed by its ``(target_kind, target_name)`` pair, with a count of unreadable ones.

        ``unreadable`` counts the indexed members whose row was gone or unparseable, so a shorter
        map is a truthful count and never a silent cut.
        """
        snapshot = await self._load_configs()
        return dict(snapshot.rows), len(snapshot.unreadable)

    async def _load_configs(self) -> StoreSnapshot[tuple[str, str], TargetConversationConfig]:
        """The config snapshot current for the map's write token, re-loaded when the token moved.

        The token is read FIRST: a write landing between it and the row reads leaves the new
        snapshot tagged with the older token, so the next read loads again.
        """
        async with client_ctx(RedisClient, self.settings.redis) as r:
            token = await awaited(r.get(self.settings.target_config_version_key))
            token = None if token is None else _as_str(token)
            held = self._snapshot
            if held is not None and held.token == token:
                return held
            members = await awaited(r.smembers(self.settings.target_config_names_key))
            member_list = sorted(_as_str(member) for member in members)
            prefix = self.settings.target_config_key_prefix
            raws = await awaited(r.mget([f"{prefix}{member}" for member in member_list])) if member_list else []
        configs: dict[tuple[str, str], TargetConversationConfig] = {}
        unreadable: set[str] = set()
        for member, raw in zip(member_list, raws, strict=True):
            if raw is None:
                # Indexed member with no row: a corrupt state (the row key never expires).
                logger.warning("conversations: config member %r is indexed but has no row; skipping", member)
                unreadable.add(member)
                continue
            try:
                config = TargetConversationConfig.model_validate_json(_as_str(raw))
            except ValueError:
                logger.warning("conversations: config member %r has an unparseable row and was skipped", member)
                unreadable.add(member)
                continue
            configs[(config.target_kind, config.target_name)] = config
        snapshot = StoreSnapshot(token=token, rows=MappingProxyType(configs), unreadable=frozenset(unreadable))
        self._snapshot = snapshot
        return snapshot


async def write_target_config(config: TargetConversationConfig, *, store: ConversationTargetConfigStore) -> bool:
    """Write ``config`` (an upsert) after its save rules; return whether the row is newly created.

    The one write service the set door and the backup restore share: a carried state binding is
    validated and its templates attached, then the row is stored. The target's existence is NOT
    judged here: it is the door's rule against the live registries, so a restored config may name
    a target a later section restores or a later registration provides.
    """
    if config.state_binding is not None:
        from tai42_skeleton.app import instance
        from tai42_skeleton.tools import state_binding

        await state_binding.validate_and_attach_binding(instance.app, config.state_binding)
    return await store.upsert(config)
