"""The per-target conversation config store — keyspace 9 of the conversation bridge: the
durable, backed-up ``(target_kind, target_name)`` → :class:`TargetConversationConfig` map
(``multichannel`` opt-in + first-contact ``greeting_template``).

Redis-backed and gated exactly as the routing-row store: construction refuses with a loud
501 without the redis conversations backend, because a durable operator-config map cannot
live per-process. The registry is INERT here — nothing reads the stored config yet; the
accept path and the pairing tool consume it in a later step.
"""

from __future__ import annotations

import logging
from typing import Any

from tai42_contract.conversations import TargetConversationConfig
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations.errors import NotSupportedError
from tai42_skeleton.utils.redis_typing import awaited, eval_script

logger = logging.getLogger(__name__)

_NO_BACKEND = "conversation target config requires the redis conversations backend"

# The per-config key write and the name-index add must be ONE atomic unit, or an upsert
# racing a delete of the same key leaves the row keyed but unindexed, or indexed but
# keyless. Every key is passed in from ``ConversationsSettings``.
#
# put: KEYS[1]=names index, KEYS[2]=the config's own key; ARGV = member, config_json.
# Returns 1 when the row already existed (a replace), 0 when it is newly created.
_PUT_LUA = """
-- conversations:config:put:atomic
local names_key, config_key = KEYS[1], KEYS[2]
local member, config_json = ARGV[1], ARGV[2]
local existed = redis.call('EXISTS', config_key)
redis.call('SET', config_key, config_json)
redis.call('SADD', names_key, member)
return existed
"""

# delete: KEYS[1]=names index, KEYS[2]=the config's own key; ARGV = member. Returns 1 when
# a row was removed, 0 when none existed.
_DELETE_LUA = """
-- conversations:config:delete:atomic
local names_key, config_key = KEYS[1], KEYS[2]
local member = ARGV[1]
local removed = redis.call('DEL', config_key)
redis.call('SREM', names_key, member)
return removed
"""


def _as_str(value: Any) -> str:
    """Normalize Redis's ``bytes``-or-``str`` return to ``str``."""
    return value.decode() if isinstance(value, bytes) else value


def _member(target_kind: str, target_name: str) -> str:
    """The index member for a config key — the row-key suffix, so appending it to
    :attr:`ConversationsSettings.target_config_key_prefix` rebuilds the row key it names."""
    return f"{target_kind}:{target_name}"


class ConversationTargetConfigStore:
    """The Redis-backed per-target config store (keyspace 9). Construction refuses with a
    loud 501 without the redis conversations backend."""

    def __init__(self, settings: ConversationsSettings) -> None:
        if settings.in_memory:
            raise NotSupportedError(_NO_BACKEND)
        self.settings = settings

    async def upsert(self, config: TargetConversationConfig) -> bool:
        """Store ``config`` (an upsert — create or replace), keeping the name index in
        lockstep. Return ``True`` when the row is newly created, ``False`` when it replaced
        an existing row of the same ``(target_kind, target_name)`` key."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            existed = await eval_script(
                r,
                _PUT_LUA,
                2,
                self.settings.target_config_names_key,
                self.settings.target_config_key(config.target_kind, config.target_name),
                _member(config.target_kind, config.target_name),
                config.model_dump_json(),
            )
        # ``existed`` truthy ⇒ a replace; falsy ⇒ a fresh create.
        return not bool(existed)

    async def get(self, target_kind: str, target_name: str) -> TargetConversationConfig | None:
        """The stored config for ``(target_kind, target_name)``, or ``None`` when none exists."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            raw = await awaited(r.get(self.settings.target_config_key(target_kind, target_name)))
        if raw is None:
            return None
        return TargetConversationConfig.model_validate_json(_as_str(raw))

    async def delete(self, target_kind: str, target_name: str) -> bool:
        """Remove the config for ``(target_kind, target_name)``, keeping the name index in
        lockstep. Return ``True`` when a row was removed, ``False`` when none existed."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            removed = await eval_script(
                r,
                _DELETE_LUA,
                2,
                self.settings.target_config_names_key,
                self.settings.target_config_key(target_kind, target_name),
                _member(target_kind, target_name),
            )
        return bool(removed)

    async def list(self) -> dict[tuple[str, str], TargetConversationConfig]:
        """Every stored config keyed by its ``(target_kind, target_name)`` pair."""
        configs: dict[tuple[str, str], TargetConversationConfig] = {}
        async with client_ctx(RedisClient, self.settings.redis) as r:
            members = await awaited(r.smembers(self.settings.target_config_names_key))
            if not members:
                return {}
            member_list = sorted(_as_str(member) for member in members)
            prefix = self.settings.target_config_key_prefix
            raws = await awaited(r.mget([f"{prefix}{member}" for member in member_list]))
        for member, raw in zip(member_list, raws, strict=True):
            if raw is None:
                # Indexed member with no row: a corrupt state (the row key never expires).
                logger.warning("conversations: config member %r is indexed but has no row; skipping", member)
                continue
            config = TargetConversationConfig.model_validate_json(_as_str(raw))
            configs[(config.target_kind, config.target_name)] = config
        return configs
