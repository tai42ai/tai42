"""Lazy accessors to the stores, registries and tool runner a turn uses.

Each returns a freshly constructed store bound to the current settings, or reaches the
running app for its agent registry and tool runner — the single seam turn code goes
through so the backing store or app is resolved once, at call time.
"""

from __future__ import annotations

from tai42_contract.agent import Agent

from tai42_skeleton.conversations.mode import ConversationModeStore
from tai42_skeleton.conversations.pair_codes import ConversationPairCodeStore
from tai42_skeleton.conversations.persons import ConversationPersonStore
from tai42_skeleton.conversations.records import ConversationRecordStore
from tai42_skeleton.conversations.redeem_throttle import ConversationRedeemThrottle
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore


def _store() -> ConversationRecordStore:
    return ConversationRecordStore(ConversationsSettings())


def _person_store() -> ConversationPersonStore:
    return ConversationPersonStore(ConversationsSettings())


def _pair_code_store() -> ConversationPairCodeStore:
    return ConversationPairCodeStore(ConversationsSettings())


def _config_store() -> ConversationTargetConfigStore:
    return ConversationTargetConfigStore(ConversationsSettings())


def _redeem_throttle() -> ConversationRedeemThrottle:
    return ConversationRedeemThrottle(ConversationsSettings())


def _agent_registry() -> dict[str, Agent]:
    from tai42_skeleton.app import instance

    return instance.app.agents.all_agents()


def _tools():
    from tai42_skeleton.app import instance

    return instance.app.tools


async def _refresh_thread_mode_ttl(thread_id: str) -> None:
    """Extend a live mode override's retention window on new thread activity.

    An override lives exactly as long as the conversation stays within its retention
    window. A no-op when none is set — the override is never resurrected.
    """
    await ConversationModeStore(ConversationsSettings()).refresh_ttl(thread_id)
